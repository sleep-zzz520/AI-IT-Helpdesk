"""转译降维核心：图片 → GLM-4V 结构化文本描述（Phase 4 多模态数据支持）。

核心思想（为什么叫"转译降维"）：
- 向量库/BM25 只认识【文本】，图片/音频/视频这些"高维数据"进不了现有检索链路
- 用多模态大模型把非文本内容"翻译"成一段结构化文本，让文本代替原文件入库
- 运维截图语义 95% 在文字上（错误码/报错信息/按钮文案），转译信息损失很小
- 代价是描述有损（模型可能看错）；联合嵌入（bge-visualized-m3/ColPali）留作
  metadata 兼容的升级项——media_ref 保存原始文件路径，将来路由给 GLM-4V 二次看图

设计要点：
1. 双通道输出：结构化字段（error_code/key_texts，供精确过滤与 Eval）+ summary
   自然语言摘要（转译后的"文本化身"，进向量检索）
2. 转译缓存：按图片内容 sha256 缓存转译结果——图片没变不重复调 LLM，
   幂等（连续两次 sync 第二次全跳过）且限流时段不拖慢同步
3. 失败返回 None 不抛异常：sync 引擎有失败自愈（台账 hash 不更新，下次重试）
"""
import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config import BASE_DIR
from app.llm import chat_with_image

# ===== 转译缓存 =====
# 位置：项目 .rag/transcribe_cache/（与 ChromaDB 持久化同级，Docker 挂卷一起落盘）
TRANSCRIBE_CACHE_DIR = Path(BASE_DIR) / ".rag" / "transcribe_cache"

# GLM-4V 单图大小上限（10MB，智谱限制；超过的图片截取中间区域降采样）
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

TRANSCRIBE_PROMPT = """你是一名 IT 运维截图分析助手。仔细看这张图片，它是 IT 运维场景的截图
（可能是报错弹窗、客户端界面、命令行输出、监控面板、浏览器页面）。
请输出 JSON（不要输出任何其他内容）：
{
  "scene": "场景分类，如 vpn_client_error / email_error / cmdline_output / dashboard / web_browser_error / other",
  "error_code": "截图中的错误码（如 800/720/1068），没有则为空字符串",
  "key_texts": ["截图中所有关键文字，如报错信息、按钮文案、弹窗标题，3-8 条"],
  "ui_state": "用一句话描述界面状态（什么软件、什么状态、用户看到的画面）",
  "summary": "用 50-100 字的中文总结这张截图表达的问题，供知识库检索使用，必须包含错误码和关键信息"
}"""


@dataclass
class TranscribeResult:
    """一次转译的结构化结果。"""
    scene: str = "other"
    error_code: str = ""
    key_texts: list[str] = field(default_factory=list)
    ui_state: str = ""
    summary: str = ""
    image_hash: str = ""        # 源图片内容指纹（缓存键）
    media_ref: str = ""         # 原始图片相对路径（回答阶段可路由 GLM-4V 二次看图）
    from_cache: bool = False    # 本次结果是否命中缓存（可观测性/调试）


def _image_hash(data: bytes) -> str:
    """图片内容 sha256 前 16 位：内容变了 → hash 变 → 重新转译（缓存失效）。

    缓存键用【原始内容指纹】（降采样前）：同一张图无论走文件路径还是
    data_url 入口，hash 一致 → 入库转译与查询转译共享同一缓存。
    """
    return hashlib.sha256(data).hexdigest()[:16]


def _prepare_image_data(data: bytes) -> bytes:
    """超限图片（>10MB）降采样：截中央 1024 区域重编码为 PNG。

    截中央而非整图缩小：报错弹窗通常居中，缩小整张会让文字糊掉；
    保证"内容不丢 + 体积达标"。
    """
    if len(data) <= _MAX_IMAGE_BYTES:
        return data
    import io

    from PIL import Image
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    box = (max(0, (w - 1024) // 2), max(0, (h - 1024) // 2),
           min(w, (w + 1024) // 2), min(h, (h + 1024) // 2))
    crop = img.crop(box)
    crop.thumbnail((1024, 1024))
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def _to_data_url(data: bytes) -> str:
    """图片 bytes → base64 data URL（chat_with_image 需要的格式）。"""
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _load_cache(image_hash: str) -> dict | None:
    """读转译缓存（.rag/transcribe_cache/<hash>.json）；损坏/缺失返回 None。"""
    try:
        path = TRANSCRIBE_CACHE_DIR / f"{image_hash}.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(image_hash: str, result: dict) -> None:
    """写转译缓存（缓存损坏不影响主流程，失败静默）。"""
    try:
        TRANSCRIBE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = TRANSCRIBE_CACHE_DIR / f"{image_hash}.json"
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _parse_result(raw: str) -> dict | None:
    """解析模型输出：提取第一个 JSON 对象并清洗字段类型。

    免费模型偶尔输出 ```json 代码块 或夹带废话，正则提取后仍按 dict 校验。
    """
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # 字段类型兜底：模型可能漏字段/给错类型，保证下游不炸
    data["scene"] = str(data.get("scene") or "other")
    data["error_code"] = str(data.get("error_code") or "")
    texts = data.get("key_texts") or []
    data["key_texts"] = [str(t) for t in texts if isinstance(t, str)] if isinstance(texts, list) else []
    data["ui_state"] = str(data.get("ui_state") or "")
    data["summary"] = str(data.get("summary") or "")
    return data


def _transcribe_bytes(data: bytes, media_ref: str = "") -> TranscribeResult | None:
    """图片 bytes → 结构化转译（缓存优先）；失败返回 None（绝不抛异常）。

    media_ref：媒体来源标识（文件相对路径 / URL），写入结果供 metadata 使用。
    这是文件入口与 data_url 入口的共用核心——同一张图两个入口共享缓存。
    """
    try:
        img_hash = _image_hash(data)

        # ---- 缓存命中：不调 LLM（幂等 + 限流友好） ----
        cached = _load_cache(img_hash)
        if cached:
            return TranscribeResult(
                **{**cached, "image_hash": img_hash, "media_ref": media_ref, "from_cache": True}
            )

        raw = chat_with_image(_to_data_url(_prepare_image_data(data)), TRANSCRIBE_PROMPT)
        parsed = _parse_result(raw)
        if not parsed:
            return None
        result = TranscribeResult(
            scene=parsed["scene"], error_code=parsed["error_code"],
            key_texts=parsed["key_texts"], ui_state=parsed["ui_state"],
            summary=parsed["summary"],
            image_hash=img_hash, media_ref=media_ref, from_cache=False,
        )
        # 缓存只存可序列化字段（不含运行时字段 from_cache/media_ref）
        _save_cache(img_hash, {
            "scene": result.scene, "error_code": result.error_code,
            "key_texts": result.key_texts, "ui_state": result.ui_state,
            "summary": result.summary,
        })
        return result
    except Exception:
        return None


def transcribe_image(image_path: Path, media_ref: str = "") -> TranscribeResult | None:
    """转译本地图片文件（知识入库入口）；失败返回 None（sync 会重试）。

    media_ref：原始文件相对 kb_root 的路径，写入 metadata 供回答阶段二次看图。
    """
    try:
        return _transcribe_bytes(image_path.read_bytes(), media_ref=media_ref)
    except OSError:
        return None


def transcribe_data_url(image_data_url: str, media_ref: str = "") -> TranscribeResult | None:
    """转译 base64 data URL（前端消息协议，查询侧入口）。

    消息里带图（data:image/png;base64,xxx）→ 转译结果拼进检索 query，
    让历史同类截图知识块被命中——「截图变知识资产」的查询侧闭环。
    """
    try:
        payload = image_data_url.split(",", 1)[1] if "," in image_data_url else image_data_url
        return _transcribe_bytes(base64.b64decode(payload), media_ref=media_ref)
    except (ValueError, TypeError):
        return None


def to_knowledge_text(result: TranscribeResult, image_path: Path) -> str:
    """转译结果 → 伪 Markdown 文档正文（复用 splitter 的 ## 结构切分）。

    为什么转成 Markdown 而不是直接存 JSON 字符串：
    - splitter 按 ## 标题切子块，标题本身成为检索语义锚点（"错误码"、"关键信息"）
    - 文本形态对 Embedding 友好（JSON 括号/引号污染语义）
    - 将来媒体类型扩展（音频/视频）只需各自实现"转译 → 文本"，链路零改动
    """
    lines = [
        f"# 截图转译：{image_path.name}",
        "",
        "## 摘要",
        result.summary,
        "",
        "## 错误码",
        result.error_code or "无",
        "",
        "## 关键信息",
    ]
    for t in result.key_texts:
        lines.append(f"- {t}")
    lines.append(f"- 界面状态：{result.ui_state}")
    lines.append(f"- 场景分类：{result.scene}")
    return "\n".join(lines)


def to_inline_text(result: TranscribeResult, image_path: Path) -> str:
    """转译结果 → 段落式内联块（原位转译用，如 md 的 ![图] 位置）。

    与 to_knowledge_text 的区别（为什么两套模板）：
    - to_knowledge_text：独立图片文档（# 开头 + ## 章节）——splitter 切成独立子块，
      图片本身是检索单元（"错误码"章节成为检索锚点）
    - to_inline_text：插入到宿主文档的原文位置——用 ### 标题，splitter 只认 ## 不切块，
      转译内容【并入所在章节】保持上下文连贯（前文"如下图所示"+ 后文"图中显示…"）
      结构化字段用加粗标签而非标题，避免引入"摘要"这类弱语义章节
    """
    lines = [
        f"### 截图转译：{image_path.name}",
        "",
        f"**摘要**：{result.summary}",
        "",
        f"**错误码**：{result.error_code or '无'}",
        "",
        "**关键信息**：",
    ]
    for t in result.key_texts:
        lines.append(f"- {t}")
    lines.append(f"- 界面状态：{result.ui_state}")
    lines.append(f"- 场景分类：{result.scene}")
    return "\n".join(lines)

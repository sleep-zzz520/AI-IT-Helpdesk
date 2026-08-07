"""知识文档加载：扫描 docs/knowledge/ 下的 Markdown，解析 frontmatter 元数据。

frontmatter 格式（YAML，`---` 包裹）：
    ---
    scenario: vpn
    error_codes: ["800", "720"]
    risk: low
    action: vpn.renew_certificate
    version: "1.0"
    status: active
    valid_to: "2099-12-31"
    tags: [vpn, 证书]
    ---
    # 正文标题
    ## 章节

这套元数据会被 sync 复制进每个 chunk 的 metadata：
检索命中后，action/risk 直接驱动现有执行流程（风险分级、工具注册表不变）。
"""
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import BASE_DIR
from app.rag.chunks import KnowledgeDoc
from app.rag.parsers import (
    ImageRef,
    ParsedDoc,
    parse_docx,
    parse_pdf,
    parse_pptx,
    parse_xlsx,
    render_pdf_pages,
)
from app.rag.transcribe import to_inline_text, to_knowledge_text, transcribe_image

# 图片扩展名（Phase 4 多模态：转译降维后以文本形态入库）
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# 支持的文档格式扩展名（Phase 4 第二步：loader 格式扩展）
_DOC_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"}

# 音频扩展名（ASR 转写：PyAV 统一转 wav，容器格式不限）
_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}

# 视频扩展名（PyAV 抽帧 + 音轨转写）
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

# 媒体文档（图片/PDF/docx/...）的合成 frontmatter 默认值：
# 这些文件没有手写 frontmatter，由 loader 补全
_DEFAULT_MEDIA_META = {
    "status": "active",
    "valid_to": "2099-12-31",  # 默认不过期；需要过期管理时人工改
}

# 内嵌图片/PDF 页面落盘目录（内容 hash 命名：同图同文件 → transcribe 缓存命中）
IMAGE_TMP_DIR = Path(BASE_DIR) / ".rag" / "image_tmp"

# Markdown 图片引用：![alt](path)（路径不含空格/括号，保守匹配真实用法）
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def doc_id_of(rel_path: str) -> str:
    """稳定 doc_id：相对路径哈希前 16 位（见 chunks.py 的设计说明）。"""
    return hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:16]


@dataclass
class LoadResult:
    """一次扫描的结果（供 sync 报告 / 调试）。"""
    docs: list[KnowledgeDoc] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # 非 .md 或 frontmatter 损坏的文件


def _parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """解析 `---` 包裹的 YAML frontmatter。

    返回 (meta, body)；无 frontmatter 返回 (None, 原文)。
    """
    if not text.startswith("---"):
        return None, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
        if not isinstance(meta, dict):
            return None, text
        return meta, parts[2].lstrip("\n")
    except yaml.YAMLError:
        return None, text  # frontmatter 损坏按无 meta 处理（sync 会跳过并报告）


def _inline_md_images(body: str, md_path: Path, kb_root: Path) -> str:
    """md 中 ![alt](path) 原位转译：图引用保留，其后插入段落式转译块。

    - 外链（http/https）、绝对路径、锚点、data URI、缺失文件：原样保留不处理
    - 图片路径相对 md 文件所在目录解析（Markdown 惯例）
    - 转译失败：保留原引用（转译是增强不是替换，图丢了不能破坏文档可读性）
    """
    def _repl(m: re.Match) -> str:
        target = m.group(2)
        if target.startswith(("http://", "https://", "/", "#", "data:")):
            return m.group(0)
        img_path = (md_path.parent / target).resolve()
        if not img_path.is_file() or img_path.suffix.lower() not in _IMAGE_EXTS:
            return m.group(0)
        try:
            rel = img_path.relative_to(kb_root.resolve()).as_posix()
        except ValueError:
            return m.group(0)  # 引用 kb 外的图片：跳过（media_ref 必须落在 kb 内）
        trans = transcribe_image(img_path, media_ref=rel)
        if trans is None:
            return m.group(0)
        return f"{m.group(0)}\n\n{to_inline_text(trans, img_path)}"

    return _MD_IMAGE_RE.sub(_repl, body)


def load_doc(path: Path, kb_root: Path) -> KnowledgeDoc | None:
    """加载单篇文档；frontmatter 缺失/损坏返回 None（sync 跳过并报告）。

    Phase 4：正文中的 ![图] 引用会在原文位置原位转译（GLM-4V 描述块插入其后）。
    """
    rel = path.resolve().relative_to(kb_root.resolve()).as_posix()
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    if meta is None or "scenario" not in meta:
        return None
    body = _inline_md_images(body, path, kb_root)
    return KnowledgeDoc(rel_path=rel, doc_id=doc_id_of(rel), meta=meta, content=body)


def load_image_doc(path: Path, kb_root: Path) -> KnowledgeDoc | None:
    """加载图片文档：GLM-4V 转译降维 → 伪 Markdown → KnowledgeDoc。

    - scenario 从图片所在目录名推断（与 .md 按场景分目录的组织方式一致）
    - media_type/media_ref 写入 metadata：回答阶段可路由 GLM-4V 二次看图
    - 转译失败返回 None：sync 跳过并报告，下次 sync 自动重试（失败自愈）
    """
    rel = path.resolve().relative_to(kb_root.resolve()).as_posix()
    trans = transcribe_image(path, media_ref=rel)
    if trans is None:
        return None
    meta = {
        "scenario": path.parent.name,
        "media_type": "image",
        "media_ref": rel,
        **_DEFAULT_MEDIA_META,
    }
    content = to_knowledge_text(trans, path)
    return KnowledgeDoc(rel_path=rel, doc_id=doc_id_of(rel), meta=meta, content=content)


def _transcribe_image_ref(ref: ImageRef, media_ref: str) -> str:
    """内嵌图片/PDF 页面 → 伪 Markdown 段落（转译失败返回空串，静默跳过）。

    图片 bytes 落盘到 IMAGE_TMP_DIR，文件名 = 内容 sha256 前 16 位：
    同图永远同文件 → transcribe.py 的内容哈希缓存自然命中（幂等，不重复调模型）。
    media_ref 带 # 后缀（如 "guide.pdf#page-3.png"）：回答阶段可精确定位原图。
    """
    key = hashlib.sha256(ref.data).hexdigest()[:16]
    tmp_path = IMAGE_TMP_DIR / f"{key}.png"
    try:
        IMAGE_TMP_DIR.mkdir(parents=True, exist_ok=True)  # write_bytes 不会自动建目录
        tmp_path.write_bytes(ref.data)
    except OSError:
        return ""
    trans = transcribe_image(tmp_path, media_ref=media_ref)
    if trans is None:
        return ""
    # 内联模板（### 段落式）：插入宿主文档的图不切断章节；
    # 独立图片文档（load_image_doc）才用 # 模板（to_knowledge_text）
    return to_inline_text(trans, Path(ref.name))


def _compose_media_doc(rel: str, media_type: str, sections: list[tuple[str, str]],
                       images: list[ImageRef], path: Path) -> KnowledgeDoc | None:
    """统一组装媒体文档：sections → ## 章节；images → 按锚点原位插入转译块。

    - img.page > 0：插回所属页码的 section 内（pptx 页内图 → 上下文连贯）
    - img.page == 0：无锚点（docx 内嵌图）→ 放文档末尾（附件式）
    全部格式（pdf/docx/pptx/xlsx/扫描件）都汇到这里——「降维」的统一出口。
    既无文本也无图片（文件损坏/解析失败）返回 None：sync 跳过并报告。
    """
    images_by_page: dict[int, list[ImageRef]] = {}
    tail_images: list[ImageRef] = []
    for img in images:
        # 锚点必须落在 sections 范围内（空白页会让页号与章节数错位）；
        # 越界/无锚点 → 文档末尾（图片内容不能丢）
        if img.page and img.page <= len(sections):
            images_by_page.setdefault(img.page, []).append(img)
        else:
            tail_images.append(img)

    parts: list[str] = []
    for idx, (title, body) in enumerate(sections, start=1):
        parts.append(f"## {title}\n\n{body}")
        for img in images_by_page.get(idx, []):
            trans_text = _transcribe_image_ref(img, media_ref=f"{rel}#{img.name}")
            if trans_text:
                parts.append(trans_text)
    for img in tail_images:
        trans_text = _transcribe_image_ref(img, media_ref=f"{rel}#{img.name}")
        if trans_text:
            parts.append(trans_text)
    if not parts:
        return None
    meta = {
        "scenario": path.parent.name,   # 与 .md 按场景分目录的组织方式一致
        "media_type": media_type,
        "media_ref": rel,
        **_DEFAULT_MEDIA_META,
    }
    content = "\n\n".join(parts)
    return KnowledgeDoc(rel_path=rel, doc_id=doc_id_of(rel), meta=meta, content=content)


def load_pdf_doc(path: Path, kb_root: Path) -> KnowledgeDoc | None:
    """加载 PDF：文本型抽文本；扫描型（分诊判定）每页渲染 → GLM-4V 转译。"""
    rel = path.resolve().relative_to(kb_root.resolve()).as_posix()
    parsed = parse_pdf(path)
    if parsed.scanned:
        pages = render_pdf_pages(path)   # 扫描件：每页变图片，走转译
        return _compose_media_doc(rel, "pdf", [], pages, path)
    return _compose_media_doc(rel, "pdf", parsed.sections, [], path)


def load_docx_doc(path: Path, kb_root: Path) -> KnowledgeDoc | None:
    """加载 Word：标题层级 + 正文 + 表格 + 内嵌图片（zip 提取 → 转译）。"""
    rel = path.resolve().relative_to(kb_root.resolve()).as_posix()
    parsed: ParsedDoc = parse_docx(path)
    return _compose_media_doc(rel, "docx", parsed.sections, parsed.images, path)


def load_pptx_doc(path: Path, kb_root: Path) -> KnowledgeDoc | None:
    """加载 PPT：每页一个章节 + 内嵌图片（转译）。"""
    rel = path.resolve().relative_to(kb_root.resolve()).as_posix()
    parsed: ParsedDoc = parse_pptx(path)
    return _compose_media_doc(rel, "pptx", parsed.sections, parsed.images, path)


def load_xlsx_doc(path: Path, kb_root: Path) -> KnowledgeDoc | None:
    """加载 Excel：每个 sheet 一个章节（Markdown 表格）。"""
    rel = path.resolve().relative_to(kb_root.resolve()).as_posix()
    parsed: ParsedDoc = parse_xlsx(path)
    return _compose_media_doc(rel, "xlsx", parsed.sections, [], path)


def load_audio_doc(path: Path, kb_root: Path) -> KnowledgeDoc | None:
    """加载音频：GLM-ASR 转写 → 伪 Markdown（转写文本即知识内容）。"""
    from app.rag.asr import transcribe_audio

    rel = path.resolve().relative_to(kb_root.resolve()).as_posix()
    text = transcribe_audio(path)
    if not text:
        return None
    return _compose_media_doc(rel, "audio", [("语音转写", text)], [], path)


def load_video_doc(path: Path, kb_root: Path) -> KnowledgeDoc | None:
    """加载视频：抽帧（GLM-4V 转译）+ 音轨（GLM-ASR 转写）。

    帧图是画面的文字载体（报错弹窗/操作演示），转写文本是语音内容，
    两者都是知识——「视频 = 会动的文档」，转译降维后与文本一视同仁。
    """
    from app.rag.video import extract_audio_text, extract_frames

    rel = path.resolve().relative_to(kb_root.resolve()).as_posix()
    frames = extract_frames(path)            # 等间隔抽帧（上限 VIDEO_MAX_FRAMES）
    audio_text = extract_audio_text(path)    # 音轨前 N 秒转写
    sections = [("语音转写", audio_text)] if audio_text else []
    return _compose_media_doc(rel, "video", sections, frames, path)


def scan_docs(kb_root: Path) -> LoadResult:
    """递归扫描 kb_root 下所有文档：md / 图片 / 文档格式 / 音频 / 视频。"""
    result = LoadResult()
    for path in sorted(kb_root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".md":
            doc = load_doc(path, kb_root)
        elif suffix in _IMAGE_EXTS:
            doc = load_image_doc(path, kb_root)
        elif suffix == ".pdf":
            doc = load_pdf_doc(path, kb_root)
        elif suffix == ".docx":
            doc = load_docx_doc(path, kb_root)
        elif suffix == ".pptx":
            doc = load_pptx_doc(path, kb_root)
        elif suffix == ".xlsx":
            doc = load_xlsx_doc(path, kb_root)
        elif suffix in _AUDIO_EXTS:
            doc = load_audio_doc(path, kb_root)
        elif suffix in _VIDEO_EXTS:
            doc = load_video_doc(path, kb_root)
        else:
            continue  # 未知格式跳过
        if doc is None:
            result.skipped.append(path.as_posix())
        else:
            result.docs.append(doc)
    return result

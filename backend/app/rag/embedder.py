"""嵌入：智谱 Embedding-3（openai SDK，与 LLM 调用同一套配置）。

为什么 Embedding-3：
- 0.5 元/百万 tokens，运维知识库规模（几百 ~ 8 万 chunk）成本 ≈ 0~24 元
- 与 GLM 同一生态（同 key），中文效果好，API 兼容 openai SDK 零新依赖
- 支持自定义维度：1024 维是精度/存储平衡点（默认 2048 存储翻倍收益甚微）

为什么批处理：单请求最多 64 条；批量能省网络往返，限流时段更稳。
"""
import time

from openai import OpenAI

from app.config import settings

EMBED_MODEL = "embedding-3"
EMBED_DIM = 1024
BATCH_SIZE = 64          # API 单请求上限
MAX_RETRIES = 3          # 免费模型限流（429）/网络抖动：重试 3 次
RETRY_BACKOFF = 2.0      # 退避基数（秒）：1s → 2s → 4s

# 输入清洗（踩坑：智谱 embedding 对空串/超长/控制字符返回 400 code 1210）
# 单条上限：Embedding-3 限 2048 tokens（中文按字约 2048 字），超长必须截断
EMBED_MAX_CHARS = 1800   # 留余量（tokens ≈ 字数的 1~1.3 倍，1800 字安全）
# 需剥离的控制字符：模型输出的异常内容可能夹带（实测崩过）
_CTRL_RE = None  # 惰性编译，避免 import 成本

_client = OpenAI(api_key=settings.ZHIPU_API_KEY, base_url=settings.GLM_BASE_URL)


def _clean_text(text: str) -> str:
    """清洗待嵌入文本：截断超长 + 剥离控制字符，避免 API 400。

    为什么加这个（踩坑）：qa_eval 的 answer_relevancy 对 LLM"反推的问题"
    做 embedding，免费模型偶发输出异常内容（空串/超长/控制字符），
    直接送 API 触发 400 code 1210（参数有误）。清洗后：
    - 空/纯空白 → 返回 ""（由 embed_texts 兜底返回零向量）
    - 超长 → 截断到 EMBED_MAX_CHARS
    - 控制字符（\x00-\x1f 等）→ 剥离
    """
    global _CTRL_RE
    if _CTRL_RE is None:
        import re
        _CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
    text = _CTRL_RE.sub("", text)
    return text[:EMBED_MAX_CHARS]


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """嵌入一批（≤64 条），失败指数退避重试。"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = _client.embeddings.create(
                model=EMBED_MODEL, input=texts, dimensions=EMBED_DIM
            )
            # OpenAI 兼容响应：data 顺序与 input 一致
            return [d.embedding for d in resp.data]
        except Exception as e:  # noqa: BLE001 嵌入失败不致命，重试后仍失败向上抛
            last_error = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF ** attempt)
    raise RuntimeError(f"Embedding 调用失败（重试 {MAX_RETRIES} 次）: {last_error}")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入任意条数文本（自动分批）。

    防御性清洗：空/空白文本返回零向量（不送 API，避免 400 崩整个流程），
    超长文本截断。零向量在余弦相似度里贡献为 0（不影响其他正常条）。
    """
    cleaned = [_clean_text(t) if t else "" for t in texts]
    zero = [0.0] * EMBED_DIM
    vectors: list[list[float]] = []
    for i in range(0, len(cleaned), BATCH_SIZE):
        batch = cleaned[i:i + BATCH_SIZE]
        # 空文本不送 API（智谱 400），原位返回零向量
        valid_idx = [j for j, t in enumerate(batch) if t.strip()]
        if not valid_idx:
            vectors.extend([zero] * len(batch))
            continue
        valid_texts = [batch[j] for j in valid_idx]
        got = _embed_batch(valid_texts)
        # 还原到原位：有效位填真实向量，空位填零向量
        result = [zero] * len(batch)
        for pos, vec in zip(valid_idx, got):
            result[pos] = vec
        vectors.extend(result)
    return vectors


def embed_text(text: str) -> list[float]:
    """嵌入单条文本（检索查询用）。"""
    return embed_texts([text])[0]

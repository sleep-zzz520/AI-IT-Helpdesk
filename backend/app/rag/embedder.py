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

_client = OpenAI(api_key=settings.ZHIPU_API_KEY, base_url=settings.GLM_BASE_URL)


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
    """批量嵌入任意条数文本（自动分批）。"""
    vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        vectors.extend(_embed_batch(texts[i:i + BATCH_SIZE]))
    return vectors


def embed_text(text: str) -> list[float]:
    """嵌入单条文本（检索查询用）。"""
    return embed_texts([text])[0]

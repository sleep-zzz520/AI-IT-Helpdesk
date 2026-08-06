"""重排：智谱 Rerank 模型对召回结果二次精排。

为什么需要重排（教程 chapter4 高级检索的标准做法）：
- 召回（BM25 + 向量 + RRF）追求"别漏"（高召回），排序质量粗糙
- 精排（Rerank）追求"排准"（高精度）：用交叉编码器计算 query 与
  每条候选的真实相关性，把最相关的排到前面
- 成本固定：只对召回后的 top-10 精排（10 条），不随库规模变慢

API：POST /paas/v4/rerank（httpx 调用，openai SDK 无此接口）
- documents 最多 128 条、单条 4096 字符
- 失败降级：返回原顺序（配置开关 RERANK_ENABLED 可关）
"""
import httpx

from app.config import settings
from app.rag.store import Hit

RERANK_MODEL = "rerank"
_MAX_DOCS = 128


def rerank(query: str, hits: list[Hit], top_n: int | None = None) -> list[Hit]:
    """按 query 相关性重排 hits（原地不改，返回新列表）。

    top_n：返回精排后前 N 条；None = 全部。
    调用失败/API 异常时原样返回（检索不能因重排失败而挂掉）。
    """
    if not hits or not settings.RERANK_ENABLED:
        return hits[:top_n] if top_n else hits

    docs = [h.text for h in hits[:_MAX_DOCS]]
    try:
        resp = httpx.post(
            f"{settings.GLM_BASE_URL}/rerank",
            headers={"Authorization": f"Bearer {settings.ZHIPU_API_KEY}"},
            json={
                "model": RERANK_MODEL,
                "query": query,
                "documents": docs,
                "top_n": top_n or len(docs),
                "return_documents": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:  # noqa: BLE001 重排失败降级：原顺序返回
        print(f"[rerank] 调用失败，降级原顺序: {type(e).__name__}: {e}")
        return hits[:top_n] if top_n else hits

    # results: [{index, relevance_score}] —— index 对应 docs 的下标（即 hits 的下标）
    order = sorted(results, key=lambda r: r.get("relevance_score", 0), reverse=True)
    ranked = [hits[r["index"]] for r in order if r["index"] < len(hits)]
    # 精排后的 score 用模型相关性分（更可信）
    for r, h in zip(order, ranked):
        h.score = round(r.get("relevance_score", 0.0), 4)
    return ranked[:top_n] if top_n else ranked

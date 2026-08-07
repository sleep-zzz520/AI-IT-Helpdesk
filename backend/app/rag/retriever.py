"""统一检索入口（Phase 2：混合检索）。

流程：查询增强（错误码提取）→ 双路召回（向量 + BM25）→ RRF 融合 → Rerank 精排（可选）

为什么混合（教程 chapter4）：
- 向量路（语义）：口语泛化（"VPN连不上" ↔ "拨号失败"）
- BM25 路（词法）：错误码/专有名词精确匹配（"800"、"certutil"）
- RRF 融合：两路排名取并集重排，发挥各自优势

对外接口不变：retrieve(query_text, scenario, top_k)——kb 节点零改动。
"""
import re
from dataclasses import dataclass, field
from datetime import date

from app.config import settings
from app.rag.bm25 import Bm25Index
from app.rag.embedder import embed_text
from app.rag.reranker import rerank
from app.rag.store import Hit, VectorStore, create_store

# 错误码形态：3~4 位数字（800 / 720 / 553 / 1068 ...）
_ERROR_CODE_RE = re.compile(r"\b(\d{3,4})\b")


@dataclass
class RetrievalStats:
    """检索过程统计（Trace / Eval 用，混合检索可观测性）。"""
    vector_recall: int = 0
    bm25_recall: int = 0
    fused_total: int = 0
    reranked: bool = False
    notes: list[str] = field(default_factory=list)
    # 调试用（知识库管理页"检索调试"）：双路各自召回明细。
    # 平时 None（不占内存）；retrieve(debug=True) 时填充
    debug_vector_hits: list | None = None
    debug_bm25_hits: list | None = None


def _build_where(scenario: str | None) -> dict:
    """向量路过滤（AND 语义）。

    valid_to 用 $gte 今天（YYYYMMDD int）：过期的知识不检索（软删除/过期机制）。
    缺失 valid_to 的旧 chunk 不匹配 $gte（被过滤）——安全方向：宁缺毋滥。
    """
    today = int(date.today().strftime("%Y%m%d"))
    conditions: list[dict] = [{"status": "active"}]
    if scenario:
        conditions.append({"scenario": scenario})
    conditions.append({"valid_to": {"$gte": today}})
    return {"$and": conditions}


def _passes_filter(meta: dict, scenario: str | None) -> bool:
    """BM25 路的过滤（与向量路 where 语义一致）。

    BM25 索引是内存结构没有 where，召回后按 metadata 过滤。
    """
    if meta.get("status") not in (None, "active"):
        return False
    if scenario and meta.get("scenario") != scenario:
        return False
    valid_to = meta.get("valid_to")
    return not (valid_to and valid_to < int(date.today().strftime("%Y%m%d")))


def _enhance_query(query: str) -> str:
    """查询增强：提取错误码（3~4 位数字）原样保留在 query 里。

    向量路不需要（语义已编码）；但 BM25 是词法匹配，错误码 token 必须
    出现在查询中才能命中——用正则提取并拼回（防分词/清洗丢失数字）。
    """
    codes = _ERROR_CODE_RE.findall(query)
    return query if not codes else f"{query} {' '.join(codes)}"


def _rrf_fuse(vector_hits: list[Hit], bm25_hits: list[Hit],
              k: float, top_k: int) -> list[Hit]:
    """加权 RRF 融合：score = Σ weight / (k + rank)，按 chunk id 对齐。"""
    fused: dict[str, dict] = {}
    for rank, hit in enumerate(vector_hits, start=1):
        e = fused.setdefault(hit.id, {"hit": hit, "score": 0.0})
        e["score"] += 1.0 / (k + rank)
    for rank, hit in enumerate(bm25_hits, start=1):
        e = fused.setdefault(hit.id, {"hit": hit, "score": 0.0})
        e["score"] += 1.0 / (k + rank)
    ranked = sorted(fused.values(), key=lambda e: e["score"], reverse=True)
    return [e["hit"] for e in ranked[:top_k]]


def _attach_parents(hits: list[Hit], store: VectorStore) -> None:
    """子块命中 → 批量取父块全文（生成上下文完整，父子块策略）。"""
    parent_ids = [h.metadata.get("parent_id") for h in hits if h.metadata.get("parent_id")]
    parent_texts = store.get_by_ids(list(dict.fromkeys(parent_ids)))
    for h in hits:
        pid = h.metadata.get("parent_id")
        if pid and pid in parent_texts:
            h.parent_text = parent_texts[pid]


def retrieve(query_text: str, scenario: str | None = None,
             top_k: int = 5, store=None, bm25=None,
             stats: RetrievalStats | None = None,
             debug: bool = False) -> list[Hit]:
    """混合检索最相关的知识块。

    - store/bm25：可注入（测试用独立库）；默认生产库
    - stats：可传入收集检索统计（Trace/评估），不传则忽略
    - debug=True：stats 里附带双路召回明细（知识库管理页"检索调试"用）
    - 命中子块：Hit.text 是子块，Hit.parent_text 是父块全文
    """
    store = store or create_store()
    stats = stats or RetrievalStats()
    enhanced = _enhance_query(query_text)

    # ---- 双路召回 ----
    vector_hits = store.query(embed_text(query_text),
                              top_k=settings.RRF_RECALL_N,
                              where=_build_where(scenario))
    stats.vector_recall = len(vector_hits)

    if bm25 is None:
        bm25 = Bm25Index(store)
    raw_bm25 = bm25.search(enhanced, top_k=settings.RRF_RECALL_N * 2)  # 留过滤余量
    bm25_hits = [Hit(id=cid, text=text, metadata=meta, score=score)
                 for cid, text, score, meta in raw_bm25
                 if _passes_filter(meta, scenario)][:settings.RRF_RECALL_N]
    stats.bm25_recall = len(bm25_hits)

    # 调试：双路召回明细（不拷贝 parent_text，父块全文太重）
    if debug:
        stats.debug_vector_hits = vector_hits
        stats.debug_bm25_hits = bm25_hits

    # ---- RRF 融合 ----
    fused = _rrf_fuse(vector_hits, bm25_hits, k=settings.RRF_K, top_k=top_k * 2)
    stats.fused_total = len(fused)

    # ---- 可选 Rerank 精排（召回 2 倍量精排取 top_k）----
    if settings.RERANK_ENABLED and fused:
        fused = rerank(enhanced, fused, top_n=top_k)
        stats.reranked = True

    hits = fused[:top_k]
    _attach_parents(hits, store)
    return hits

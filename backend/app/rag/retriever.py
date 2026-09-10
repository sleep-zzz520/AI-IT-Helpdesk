"""统一检索入口（Phase 2：混合检索；Phase 5：多库联合）。

流程：查询增强（错误码提取）→ 双路召回（向量 + BM25）→ RRF 融合 → Rerank 精排（可选）

为什么混合（教程 chapter4）：
- 向量路（语义）：口语泛化（"VPN连不上" ↔ "拨号失败"）
- BM25 路（词法）：错误码/专有名词精确匹配（"800"、"certutil"）
- RRF 融合：两路排名取并集重排，发挥各自优势

多库联合（Phase 5）：对话问答可同时检索在岗库 + 配置的独立语料库
（KB_EXTRA_COLLECTIONS）——一个入口覆盖多库，跨库 RRF 融合；
不相关库靠 scenario 过滤与分数排序自然剔除。
"""
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date

from app.config import settings
from app.rag.bm25 import Bm25Index
from app.rag.embedder import embed_text
from app.rag.reranker import rerank
from app.rag.store import Hit, VectorStore, create_store, list_collections

logger = logging.getLogger(__name__)

# 错误码形态：3~4 位数字（800 / 720 / 553 / 1068 ...）
_ERROR_CODE_RE = re.compile(r"\b(\d{3,4})\b")

# BM25 索引模块级缓存：按 collection 名复用（Bm25Index 内部有 doc_count
# 失效重建机制，缓存安全）。为什么必须缓存：索引构建 = 全量 jieba 分词，
# 千级 2.5s / 万级更久——每次 retrieve 新建对象会每次都重建（实测 3s 检索，
# 缓存后 ~100ms，30 倍差距）。
_BM25_CACHE: dict[str, "Bm25Index"] = {}


def _get_bm25(store) -> Bm25Index:
    """取该 store 对应的 BM25 索引（复用缓存实例）。"""
    key = store.collection_name  # ChromaStore 暴露；蓝绿切换后 collection 名
    # 不同 → 新 key → 自动建新索引（旧索引随 GC 释放）
    if key not in _BM25_CACHE:
        _BM25_CACHE[key] = Bm25Index(store)
    return _BM25_CACHE[key]


@dataclass
class RetrievalStats:
    """检索过程统计（Trace / Eval 用，混合检索可观测性）。"""
    vector_recall: int = 0
    bm25_recall: int = 0
    fused_total: int = 0
    reranked: bool = False
    notes: list[str] = field(default_factory=list)
    # 每库召回统计（多库联合）：{collection名: {"vector_recall": n, "bm25_recall": n}}
    per_collection: dict[str, dict[str, int]] = field(default_factory=dict)
    # 调试用（知识库管理页"检索调试"）：双路各自召回明细。
    # 平时 None（不占内存）；retrieve(debug=True) 时填充
    debug_vector_hits: list | None = None
    debug_bm25_hits: list | None = None


def _build_where(scenario: str | None) -> dict:
    """向量路过滤（AND 语义）。

    valid_to 用 $gte 今天（YYYYMMDD int）：过期的知识不检索（软删除/过期机制）。
    缺失 valid_to 的旧 chunk 不匹配 $gte（被过滤）——安全方向：宁缺毋滥。

    父子块策略中，父块只负责为已命中的子块补齐生成上下文，不能参与召回
    排名。否则一篇全文父块会与其子块竞争，RRF 按文档去重后还可能保留
    不包含答案的父块或错误子块，掩盖细粒度命中问题。
    """
    today = int(date.today().strftime("%Y%m%d"))
    conditions: list[dict] = [{"status": "active"}]
    conditions.append({"chunk_type": {"$ne": "parent"}})
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
    # 与向量路一致：父块仅用于 _attach_parents，不是可排序的召回证据。
    # 兼容历史上没有 chunk_type 的旧数据；新同步数据均明确标为 child/parent。
    if meta.get("chunk_type") == "parent":
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
    """加权 RRF 融合：score = Σ weight / (k + rank)，按 chunk id 对齐。

    融合时给保留的 hit 实例打检索路标（routes）——向量/BM25 双路命中
    合并为 both。路标不参与分数计算，只作证据强度的规则层信号
    （judge 缺席时 BM25 词法命中 = 确定性相关，见 rag_query）。

    文档级去重（2026-08-12 加，Eval 真实化发现）：
    同一篇文档的多个 chunk 在融合时分数叠加霸榜，挤占其他文档的
    证据位置（实测：BM25 对泛词命中同一文档 2 个 chunk 排 #1#2，
    把答案文档挤出 top-5）。候选已在双路召回阶段限定为子块，命中的
    子块会由 _attach_parents 补上父块全文。故按 source_url 去重，每篇
    文档只保留得分最高的一个子块。
    """
    fused: dict[str, dict] = {}
    for rank, hit in enumerate(vector_hits, start=1):
        e = fused.setdefault(hit.id, {"hit": hit, "score": 0.0})
        e["hit"].routes.add("vector")
        e["score"] += 1.0 / (k + rank)
    for rank, hit in enumerate(bm25_hits, start=1):
        e = fused.setdefault(hit.id, {"hit": hit, "score": 0.0})
        e["hit"].routes.add("bm25")
        e["score"] += 1.0 / (k + rank)
    ranked = sorted(fused.values(), key=lambda e: e["score"], reverse=True)
    deduped: list[Hit] = []
    seen_docs: set[str] = set()
    for e in ranked:
        url = e["hit"].metadata.get("source_url")
        key = url or e["hit"].id  # 无 source_url 的 chunk 退化为 id 级去重
        if key in seen_docs:
            continue
        seen_docs.add(key)
        deduped.append(e["hit"])
        if len(deduped) >= top_k:
            break
    return deduped


def _attach_parents(hits: list[Hit], store: VectorStore) -> None:
    """子块命中 → 批量取父块全文（生成上下文完整，父子块策略）。"""
    parent_ids = [h.metadata.get("parent_id") for h in hits if h.metadata.get("parent_id")]
    parent_texts = store.get_by_ids(list(dict.fromkeys(parent_ids)))
    for h in hits:
        pid = h.metadata.get("parent_id")
        if pid and pid in parent_texts:
            h.parent_text = parent_texts[pid]


def _recall_store(store: VectorStore, bm25: Bm25Index | None, enhanced: str,
                  query_vec: list[float] | None, scenario: str | None) -> tuple[list[Hit], list[Hit]]:
    """单库双路召回（向量 + BM25），单库/联合共用。

    向量路分数门槛（VECTOR_MIN_SCORE）：过滤"弱相关"命中——
    实测 0.43 的弱命中会让模型基于无关文档瞎编回答（答非所问）；
    BM25 路不做分数过滤（词法命中至少词面重叠，可信度由 judge 把关）。
    query_vec=None（embedding 失败降级）→ 跳过向量路，仅 BM25 词法召回。
    """
    if query_vec is None:
        vector_hits: list[Hit] = []
    else:
        vector_hits = [h for h in store.query(query_vec, top_k=settings.RRF_RECALL_N,
                                              where=_build_where(scenario))
                       if h.score >= settings.VECTOR_MIN_SCORE]
    if bm25 is None:
        bm25 = _get_bm25(store)  # 模块级缓存（实测：不缓存每次重建 = 3s 检索）
    raw_bm25 = bm25.search(enhanced, top_k=settings.RRF_RECALL_N * 2)  # 留过滤余量
    bm25_hits = [Hit(id=cid, text=text, metadata=meta, score=score)
                 for cid, text, score, meta in raw_bm25
                 if _passes_filter(meta, scenario)][:settings.RRF_RECALL_N]
    return vector_hits, bm25_hits


def _tag_collection(hits: list[Hit], name: str) -> None:
    """给命中打来源库标。放 Hit.source_collection 而非 metadata——
    Bm25Index._metas 持有 metadata 引用，原地写会污染 BM25 缓存。"""
    for h in hits:
        h.source_collection = name


# 已存在 collection 的 TTL 缓存（联合检索存在性预检用）
_EXTRA_CHECK_TTL = 60.0
_existing_cache: dict = {"dir": None, "ts": 0.0, "names": set()}


def _existing_collections() -> set[str]:
    """当前 persist_dir 下已存在的 collection 名（60s TTL）。

    为什么预检：create_store(name) 走 get_or_create，配置拼错会【静默建空库】
    并落盘——存在性预检让拼错的名字被跳过 + 告警，绝不自动建库。
    """
    now = time.monotonic()
    cache = _existing_cache
    if cache["dir"] != settings.KB_VECTOR_DIR or now - cache["ts"] > _EXTRA_CHECK_TTL:
        cache["names"] = set(list_collections(settings.KB_VECTOR_DIR))
        cache["dir"] = settings.KB_VECTOR_DIR
        cache["ts"] = now
    return cache["names"]


def _joint_store_pairs() -> list[tuple[str, VectorStore, None]]:
    """在岗库 + 配置的独立库（多库联合的检索目标集）。"""
    active = create_store()
    pairs = [(active.collection_name, active, None)]
    for name in settings.extra_collections:  # config 层已排除蓝绿/在岗/重复
        if name not in _existing_collections():
            logger.warning("联合检索库 %s 不存在（未创建），跳过", name)
            continue
        pairs.append((name, create_store(collection_name=name), None))
    return pairs


def _attach_parents_multi(hits: list[Hit], stores: dict[str, VectorStore]) -> None:
    """跨库父块回填：按来源库分组，各库只取自己的父块（子块命中不回填错库）。"""
    groups: dict[str, list[Hit]] = {}
    for h in hits:
        groups.setdefault(h.source_collection, []).append(h)
    for name, group in groups.items():
        store = stores.get(name) or next(iter(stores.values()))  # 防御：无标回落
        _attach_parents(group, store)


def retrieve(query_text: str, scenario: str | None = None,
             top_k: int = 5, store=None, bm25=None,
             stats: RetrievalStats | None = None,
             debug: bool = False,
             joint: bool = True) -> list[Hit]:
    """混合检索最相关的知识块（单库 / 跨库联合）。

    - store/bm25：可注入（测试用独立库）；默认生产库
    - stats：可传入收集检索统计（Trace/评估），不传则忽略
    - debug=True：stats 里附带双路召回明细（知识库管理页"检索调试"用）
    - joint=True 且 store/bm25 均未注入 → 在岗库 + settings.extra_collections
      全部独立库联合检索（跨库 RRF 融合；配置为空时行为与旧版逐位一致）
    - joint=False → 只检索在岗库（kb 故障路径：执行决策不引入独立语料）
    - 命中子块：Hit.text 是子块，Hit.parent_text 是父块全文；
      Hit.source_collection 标记命中来源库
    """
    stats = stats or RetrievalStats()
    enhanced = _enhance_query(query_text)
    # 联合检索也只算一次向量，各库复用。
    # embedding 失败降级（系统性修复）：付费 embedding API 也会限流/超时，
    # 挂掉不应让检索瘫痪——降级 BM25-only（本地内存索引零 API，词法路径
    # 仍可命中错误码/专有名词），stats.notes 记录原因供 Trace 可观测
    try:
        query_vec = embed_text(query_text)
    except Exception as e:
        query_vec = None
        stats.notes.append(f"embedding 失败，降级 BM25-only: {type(e).__name__}")

    if store is None and bm25 is None and joint:
        pairs = _joint_store_pairs()
    else:
        s = store or create_store()
        pairs = [(s.collection_name, s, bm25)]

    # ---- 每库双路召回 ----
    all_vector: list[Hit] = []
    all_bm25: list[Hit] = []
    for name, s, bm25_obj in pairs:
        vec, bm25_hits = _recall_store(s, bm25_obj, enhanced, query_vec, scenario)
        _tag_collection(vec + bm25_hits, name)
        all_vector.extend(vec)
        all_bm25.extend(bm25_hits)
        stats.per_collection[name] = {
            "vector_recall": len(vec), "bm25_recall": len(bm25_hits)}
    stats.vector_recall = sum(v["vector_recall"] for v in stats.per_collection.values())
    stats.bm25_recall = sum(v["bm25_recall"] for v in stats.per_collection.values())

    # 调试：双路召回明细（不拷贝 parent_text，父块全文太重）
    if debug:
        stats.debug_vector_hits = all_vector
        stats.debug_bm25_hits = all_bm25

    # ---- RRF 融合（跨库合并：按 chunk id 对齐，同 id 分数叠加）----
    fused = _rrf_fuse(all_vector, all_bm25, k=settings.RRF_K, top_k=top_k * 2)
    stats.fused_total = len(fused)

    # ---- 可选 Rerank 精排（召回 2 倍量精排取 top_k）----
    if settings.RERANK_ENABLED and fused:
        fused = rerank(enhanced, fused, top_n=top_k)
        stats.reranked = True

    hits = fused[:top_k]
    _attach_parents_multi(hits, {name: s for name, s, _ in pairs})
    return hits

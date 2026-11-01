"""多库联合检索测试（离线，零 API 调用）。

覆盖 Phase 5 联合检索的核心承诺：
- 运维 query（带 scenario）→ 语料库被过滤，结果全来自运维库
- 语料 query → 命中语料库（联合生效）
- joint=False → 只查在岗库（执行路径安全边界）
- 配置不存在的库名 → 跳过且不自动建空库
- 默认空配置 → 行为与旧版一致（只查在岗库）

为什么用伪向量：测试目标是【路径行为】（联合/过滤/来源标记/隔离），
不是检索质量（那是 rag_eval 的职责）——伪向量零 API 成本、确定性可复现。
"""
import hashlib

import pytest

from app.config import settings
from app.rag import retriever
from app.rag.chunks import Chunk
from app.rag.store import ChromaStore, list_collections


# 确定性伪向量：同文本同向量（模拟语义编码；8 维足够区分测试文本）
def _fake_vec(text: str, dim: int = 8) -> list[float]:
    h = hashlib.sha256(text.encode()).hexdigest()
    return [int(h[i * 2:i * 2 + 2], 16) / 255 - 0.5 for i in range(dim)]


@pytest.fixture()
def joint_env(tmp_path, monkeypatch):
    """隔离环境：tmp 目录双库（运维 kb_docs / 语料 kb_eval_b）+ 伪向量。"""
    monkeypatch.setattr(settings, "KB_VECTOR_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "KB_EXTRA_COLLECTIONS", "kb_eval_b")
    monkeypatch.setattr(retriever, "embed_text", _fake_vec)
    # 清缓存：BM25 索引（按 collection 名全局）与存在性预检（按目录）
    retriever._BM25_CACHE.clear()
    retriever._existing_cache.update({"dir": None, "ts": 0.0, "names": set()})

    def _mk(collection: str, docs: list[tuple[str, str, dict]]) -> ChromaStore:
        """建库：docs = [(文本, scenario, 附加 metadata)]，父子块各一条。"""
        store = ChromaStore(persist_dir=str(tmp_path), collection_name=collection)
        chunks, vectors = [], []
        for i, (text, scenario, extra) in enumerate(docs):
            meta = {"scenario": scenario, "status": "active",
                    "valid_to": 20991231, "source_url": f"{collection}/{i}.md",
                    "doc_id": f"doc{i}", **extra}
            for kind, cid in (("parent", f"d{i}:parent"), ("child", f"d{i}:1")):
                chunks.append(Chunk(id=cid, text=text if kind == "parent" else text + " 子块", metadata=meta))
                vectors.append(_fake_vec(chunks[-1].text))
        store.upsert_chunks(chunks, vectors)
        return store

    # 运维文档建在 kb_docs（create_store() 默认在岗库名）；
    # 语料库建在 kb_eval_b（KB_EXTRA_COLLECTIONS 配置的独立库）。
    # 为什么每库 ≥4 篇：rank_bm25 的 idf 公式（log((N-n+0.5)/(n+0.5))，无 +1）
    # 在极小语料（N≤2）下所有词 idf 为负 → 总分负被 scores>0 过滤 → 空召回。
    # 干扰文档稀释后查询词 n=1、N=4 → idf 为正（真实千级库无此问题）。
    _mk("kb_docs", [
        ("VPN 报错 800 证书过期 如何续期", "vpn", {}),
        ("VPN 客户端配置步骤", "vpn", {}),
        ("邮箱 IMAP 配置指南", "email", {}),
        ("软件安装激活流程", "software", {}),
    ])
    _mk("kb_eval_b", [
        ("公司发放年终奖的通知", "scale", {}),
        ("新能源汽车销量新闻", "scale", {}),
        ("芯片行业研报摘要", "scale", {}),
        ("天气预报预警", "scale", {}),
    ])
    return tmp_path


def test_ops_query_not_polluted_by_corpus(joint_env):
    """运维 query + scenario=vpn → 结果全来自运维库，语料库双路 0 召回。"""
    stats = retriever.RetrievalStats()
    hits = retriever.retrieve("VPN 报错 800 证书过期 怎么处理", scenario="vpn",
                              top_k=5, stats=stats, debug=True)
    assert hits, "应命中运维文档"
    assert all(h.source_collection == "kb_docs" for h in hits), \
        f"语料库污染了结果: {[h.source_collection for h in hits]}"
    assert stats.per_collection["kb_eval_b"] == {"vector_recall": 0, "bm25_recall": 0}, \
        "语料库应被 scenario 过滤"


def test_corpus_query_hits_corpus(joint_env):
    """语料 query（无 scenario）→ 联合检索生效（两库都查 + 语料库词法命中）。

    断言口径说明：伪向量无真实语义，"最相似"是偶然的——本测试只验证
    【检索路径】：①两个库都被检索；②语料库有词法命中（BM25 确定性）；
    ③来源标记正确。排序质量由真实库联测/rag_eval 负责（非伪向量职责）。
    """
    stats = retriever.RetrievalStats()
    retriever.retrieve("公司年终奖什么时候发", top_k=5, stats=stats, debug=True)
    assert set(stats.per_collection) == {"kb_docs", "kb_eval_b"}, \
        f"联合应查两个库: {stats.per_collection}"
    assert stats.per_collection["kb_eval_b"]["bm25_recall"] > 0, \
        f"BM25 路应命中语料库: {stats.per_collection}"
    assert any(h.source_collection == "kb_eval_b"
               for h in (stats.debug_bm25_hits or [])), "语料库命中应带来源标记"


def test_joint_false_single_store(joint_env):
    """joint=False → 只查在岗库（执行路径安全边界）。"""
    stats = retriever.RetrievalStats()
    hits = retriever.retrieve("公司年终奖什么时候发", top_k=3,
                              stats=stats, joint=False)
    assert set(stats.per_collection) == {"kb_docs"}, \
        f"joint=False 只应查在岗库: {stats.per_collection}"
    assert all(h.source_collection == "kb_docs" for h in hits)


def test_missing_extra_collection_skipped(joint_env, monkeypatch):
    """配置不存在的库名 → 跳过 + 不自动建空库（防拼错）。"""
    monkeypatch.setattr(settings, "KB_EXTRA_COLLECTIONS", "kb_eval_nonexistent")
    before = set(list_collections(str(joint_env)))
    hits = retriever.retrieve("公司年终奖", top_k=3)
    after = set(list_collections(str(joint_env)))
    assert before == after, "不存在库不应被创建"
    assert isinstance(hits, list)


def test_empty_config_legacy_behavior(joint_env, monkeypatch):
    """默认空配置 → 只查在岗库（与旧版行为一致）。"""
    monkeypatch.setattr(settings, "KB_EXTRA_COLLECTIONS", "")
    stats = retriever.RetrievalStats()
    retriever.retrieve("VPN 报错 800", scenario="vpn", top_k=3, stats=stats)
    assert set(stats.per_collection) == {"kb_docs"}, \
        f"空配置应只查在岗库: {stats.per_collection}"


def test_debug_exposes_rrf_candidates_before_final_top_k(joint_env):
    """调试信息必须保留 RRF 候选层，才能定位候选漏召回还是最终截断。"""
    stats = retriever.RetrievalStats()
    hits = retriever.retrieve("VPN 报错 800", scenario="vpn", top_k=1,
                              stats=stats, debug=True)
    assert stats.debug_vector_hits is not None
    assert stats.debug_bm25_hits is not None
    assert stats.debug_fused_hits is not None
    assert stats.debug_final_hits is not None
    assert len(stats.debug_fused_hits) >= len(stats.debug_final_hits)
    assert [hit.id for hit in stats.debug_final_hits] == [hit.id for hit in hits]


def test_vector_only_matched_reuses_serving_threshold_and_source_cap(monkeypatch):
    """公平基线必须复用线上阈值和“每来源两段”的子 Chunk 策略。"""
    def unexpected_embedding(_query):
        raise AssertionError("已提供 query_vector 时不应重复调用 Embedding")

    monkeypatch.setattr(retriever, "embed_text", unexpected_embedding)

    class StubStore:
        collection_name = "stub"

        def query(self, _vector, top_k, where):
            assert top_k == settings.RRF_RECALL_N
            assert where == retriever._build_where(None)
            return [
                retriever.Hit("a:1", "## A", {"source_url": "a.md"}, 0.9),
                retriever.Hit("a:2", "## A2", {"source_url": "a.md"}, 0.8),
                retriever.Hit("b:1", "## B", {"source_url": "b.md"}, 0.7),
                retriever.Hit("c:1", "## C", {"source_url": "c.md"}, 0.1),
            ]

        def get_by_ids(self, _ids):
            return {}

    stats = retriever.RetrievalStats()
    hits = retriever.retrieve_vector_only(
        "测试 query", top_k=5, store=StubStore(), stats=stats, debug=True,
        query_vector=_fake_vec("测试 query"),
    )
    assert [hit.id for hit in hits] == ["a:1", "a:2", "b:1"]
    assert [hit.id for hit in stats.debug_vector_hits] == ["a:1", "a:2", "b:1"]
    assert [hit.id for hit in stats.debug_final_hits] == ["a:1", "a:2", "b:1"]


def test_hybrid_reuses_a_supplied_query_vector(joint_env, monkeypatch):
    """评测共享向量时，混合链必须跳过自己的二次 Embedding 调用。"""
    monkeypatch.setattr(retriever, "embed_text", lambda _: pytest.fail("不应重复 Embedding"))

    hits = retriever.retrieve(
        "VPN 报错 800 证书过期 怎么处理",
        scenario="vpn",
        top_k=3,
        query_vector=_fake_vec("VPN 报错 800 证书过期 怎么处理"),
    )

    assert hits


def test_rrf_weight_can_promote_a_route_without_changing_default_behavior():
    """权重只改变双路的相对贡献；默认参数仍保持历史等权排序。"""
    vector_hit = retriever.Hit(
        "vector:1", "向量命中", {"source_url": "vector.md"}, 0.9,
    )
    bm25_hit = retriever.Hit(
        "bm25:1", "BM25 命中", {"source_url": "bm25.md"}, 1.0,
    )

    default_hits = retriever._rrf_fuse(
        [vector_hit], [bm25_hit], k=60, top_k=2,
    )
    weighted_hits = retriever._rrf_fuse(
        [vector_hit], [bm25_hit], k=60, top_k=2,
        vector_weight=2.0, bm25_weight=1.0,
    )

    assert [hit.id for hit in default_hits] == ["vector:1", "bm25:1"]
    assert [hit.id for hit in weighted_hits] == ["vector:1", "bm25:1"]
    # 两路同 rank 时，默认同分保持向量输入顺序；提高 BM25 权重后可验证
    # 公式确实生效，而不仅是给 Settings 增加了一个未使用的字段。
    bm25_promoted = retriever._rrf_fuse(
        [vector_hit], [bm25_hit], k=60, top_k=2,
        vector_weight=1.0, bm25_weight=2.0,
    )
    assert [hit.id for hit in bm25_promoted] == ["bm25:1", "vector:1"]


def test_rerank_receives_chunks_before_source_limit(monkeypatch):
    """Rerank 必须能看到被来源上限隐藏的同文档答案段。"""
    class StubStore:
        collection_name = "stub"

        def get_by_ids(self, _ids):
            return {}

    def hit(chunk_id: str, source: str) -> retriever.Hit:
        return retriever.Hit(chunk_id, chunk_id, {"source_url": source}, 0.9)

    all_hits = [
        hit("guide:generic", "guide.md"),
        hit("guide:answer", "guide.md"),
        hit("other:1", "other.md"),
    ]
    monkeypatch.setattr(
        retriever,
        "_recall_store",
        lambda *_args: (list(all_hits), []),
    )
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_CANDIDATE_N", 3)
    monkeypatch.setattr(settings, "RAG_MAX_CHUNKS_PER_SOURCE", 1)

    observed: list[str] = []

    def fake_rerank(_query, hits, top_n):
        observed.extend(hit.id for hit in hits)
        assert top_n == 3
        return [hits[1], hits[0], hits[2]]

    monkeypatch.setattr(retriever, "rerank", fake_rerank)
    hits = retriever.retrieve(
        "测试 query", store=StubStore(), query_vector=_fake_vec("测试 query"), top_k=2,
    )

    assert observed == ["guide:generic", "guide:answer", "other:1"]
    assert [hit.id for hit in hits] == ["guide:answer", "other:1"]


def test_free_rrf_candidate_pool_defaults_to_legacy_and_can_expand(monkeypatch):
    """免费路径默认仍是 top_k*2；实验参数只能扩大中间池，不能缩小它。"""
    class StubStore:
        collection_name = "stub"

        def get_by_ids(self, _ids):
            return {}

    hits = [
        retriever.Hit(f"doc:{index}", f"文本 {index}", {"source_url": f"{index}.md"}, 0.9)
        for index in range(20)
    ]
    monkeypatch.setattr(retriever, "_recall_store", lambda *_args: (list(hits), []))
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    captured: list[int] = []

    def fake_fuse(*_args, top_k, **_kwargs):
        captured.append(top_k)
        return list(hits)

    monkeypatch.setattr(retriever, "_rrf_fuse", fake_fuse)

    monkeypatch.setattr(settings, "RRF_FUSION_CANDIDATE_N", 0)
    retriever.retrieve("测试 query", store=StubStore(), query_vector=_fake_vec("测试 query"), top_k=3)
    monkeypatch.setattr(settings, "RRF_FUSION_CANDIDATE_N", 16)
    retriever.retrieve("测试 query", store=StubStore(), query_vector=_fake_vec("测试 query"), top_k=3)

    assert captured == [6, 16]

"""RAG 检索与排序评测：以 Gold Chunk 计算 Recall@k 与 MRR。

评测集并非只保存“问题 → 文档”。每条 60 条检索样本均包含标准子 Chunk、
参考答案、是否可回答、问题类型和难度；本脚本只衡量检索/排序层，生成、
澄清和拒答由 ``qa_eval.py`` 使用同一份标注做端到端评测。

报告同时保留三组基线：原始纯向量、同线上候选口径的纯向量、混合检索。混合
结果会逐案例记录向量、BM25、RRF 和最终 Top-K 各阶段，避免总分掩盖坏例。

用法（backend 目录）：python -m tests.rag_eval --tmp-kb
输出：tests/rag_report.json
"""
import json
from pathlib import Path

from app.rag.embedder import embed_text
from app.rag.retriever import (
    RetrievalStats,
    _build_where,
    retrieve,
    retrieve_vector_only,
)
from app.rag.store import create_store, list_collections
from tests.rag_eval_dataset import (
    EXPECTED_RETRIEVAL_CASES,
    RETRIEVAL_CASES,
    dataset_summary,
    resolve_gold_chunk_ids,
)

# 保留这个导出名，供调用方明确获得“带标注的 60 条检索样本”。
CASES = RETRIEVAL_CASES
REPORT_PATH = Path(__file__).resolve().parent / "rag_report.json"


def _recall_mrr(hits: list, gold_chunk_ids: frozenset[str]) -> tuple[dict[int, float], float]:
    """按真实 Chunk ID 评分，而不是只要同一 source_url 就算命中。"""
    recalls: dict[int, float] = {}
    hit_ids = [hit.id for hit in hits]
    for k in (1, 3, 5):
        recalled = len(set(hit_ids[:k]) & gold_chunk_ids)
        recalls[k] = recalled / len(gold_chunk_ids)
    first = next((i for i, chunk_id in enumerate(hit_ids) if chunk_id in gold_chunk_ids), None)
    return recalls, 0.0 if first is None else 1.0 / (first + 1)


def _hit_out(hit) -> dict:
    """报告只存定位信息，不把 parent_text 等大段生成上下文重复写入 JSON。"""
    section = next(
        (line.removeprefix("## ").strip() for line in hit.text.splitlines()
         if line.startswith("## ")),
        None,
    )
    return {
        "chunk_id": hit.id,
        "source_url": hit.metadata.get("source_url"),
        "section": section,
        "score": round(hit.score, 4),
        "routes": sorted(hit.routes),
    }


def _first_gold_rank(hits: list, gold_chunk_ids: frozenset[str]) -> int | None:
    return next(
        (rank for rank, hit in enumerate(hits, start=1) if hit.id in gold_chunk_ids),
        None,
    )


def _stage_out(hits: list, gold_chunk_ids: frozenset[str]) -> dict:
    return {
        "gold_recalled": _first_gold_rank(hits, gold_chunk_ids) is not None,
        "first_gold_rank": _first_gold_rank(hits, gold_chunk_ids),
        "hits": [_hit_out(hit) for hit in hits],
    }


def _hybrid_diagnosis(stats: RetrievalStats, hits: list,
                      gold_chunk_ids: frozenset[str]) -> dict:
    """定位 Gold Chunk 是在哪个阶段消失，结论只描述可观测事实。"""
    vector = stats.debug_vector_hits or []
    bm25 = stats.debug_bm25_hits or []
    fused = stats.debug_fused_hits or []
    final = stats.debug_final_hits or hits
    stages = {
        "vector_candidates": _stage_out(vector, gold_chunk_ids),
        "bm25_candidates": _stage_out(bm25, gold_chunk_ids),
        "rrf_candidates": _stage_out(fused, gold_chunk_ids),
        "final_top_k": _stage_out(final, gold_chunk_ids),
    }
    if stages["final_top_k"]["gold_recalled"]:
        failure_stage = "final_hit"
    elif stages["rrf_candidates"]["gold_recalled"]:
        failure_stage = "lost_at_final_selection"
    elif stages["vector_candidates"]["gold_recalled"] or stages["bm25_candidates"]["gold_recalled"]:
        failure_stage = "lost_before_rrf_candidate_pool"
    else:
        failure_stage = "not_recalled_by_either_route"
    return {
        "failure_stage": failure_stage,
        "reranked": stats.reranked,
        "notes": stats.notes,
        "stages": stages,
    }


def _evaluate(name: str, search_fn, gold_ids_by_case: dict[str, frozenset[str]]) -> dict:
    """计算 Gold Chunk Recall@k 与第一个 Gold Chunk 的 MRR。"""
    recall_sums = {k: 0.0 for k in (1, 3, 5)}
    mrr_sum = 0.0
    per_case = []
    for case in RETRIEVAL_CASES:
        hits, diagnosis = search_fn(case)
        hits = hits[:5]
        recalls, mrr = _recall_mrr(hits, gold_ids_by_case[case.case_id])
        for k, score in recalls.items():
            recall_sums[k] += score
        mrr_sum += mrr
        item = {
            "case_id": case.case_id,
            "question_type": case.question_type,
            "difficulty": case.difficulty,
            "gold_chunk_ids": sorted(gold_ids_by_case[case.case_id]),
            "retrieved_chunk_ids": [hit.id for hit in hits],
            "recall_at_5": recalls[5],
        }
        if diagnosis is not None:
            item["diagnosis"] = diagnosis
        per_case.append(item)

    n = len(RETRIEVAL_CASES)
    return {
        "name": name,
        "cases": n,
        "gold_chunk_recall@1": round(recall_sums[1] / n, 3),
        "gold_chunk_recall@3": round(recall_sums[3] / n, 3),
        "gold_chunk_recall@5": round(recall_sums[5] / n, 3),
        "mrr": round(mrr_sum / n, 3),
        "per_case": per_case,
    }


def _write_report(report_path: Path, results: list[dict], *, collection: str) -> None:
    """将本次运行写到显式路径，避免覆盖基线报告或并行运行的结果。"""
    report_path.write_text(json.dumps({
        "task": "rag_gold_chunk_retrieval",
        "scope": "unfiltered_global_retrieval",
        "collection": collection,
        "methods": {
            "vector_only_raw": "原始纯向量 Top-5，保留历史比较口径。",
            "vector_only_matched": "纯向量，但复用线上 VECTOR_MIN_SCORE、RRF_RECALL_N 和文档去重。",
            "hybrid": "向量 + BM25 + RRF；每条样本附带分阶段诊断。",
        },
        "cases": len(RETRIEVAL_CASES),
        "dataset": dataset_summary(RETRIEVAL_CASES),
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    import argparse
    import tempfile

    from app.config import settings
    from app.rag.store import ChromaStore
    from app.rag.sync import run_sync

    parser = argparse.ArgumentParser()
    parser.add_argument("--tmp-kb", action="store_true", help="用独立临时库重建后评测")
    parser.add_argument(
        "--collection",
        default=None,
        help="指定已存在的 collection；默认评测当前在岗库",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=REPORT_PATH,
        help="报告输出路径；默认覆盖 tests/rag_report.json",
    )
    args = parser.parse_args()
    report_path = args.report_path
    if not report_path.parent.is_dir():
        parser.error(f"报告目录不存在：{report_path.parent}")
    if args.tmp_kb and args.collection:
        parser.error("--tmp-kb 与 --collection 不能同时使用")

    if args.tmp_kb:
        tmp = Path(tempfile.mkdtemp(prefix="kb_eval_"))
        store = ChromaStore(persist_dir=str(tmp / "chroma"), collection_name="kb_eval")
        report = run_sync(Path(settings.KB_ROOT), store, kb_name="eval_tmp")
        print(f"临时库构建: {report.summary}")
    elif args.collection:
        if args.collection not in list_collections(settings.KB_VECTOR_DIR):
            parser.error(f"collection 不存在：{args.collection}")
        store = create_store(collection_name=args.collection)
    else:
        store = create_store()

    gold_ids_by_case = resolve_gold_chunk_ids(RETRIEVAL_CASES, Path(settings.KB_ROOT))
    print(f"评测 collection: {store.collection_name}")
    print(f"库内 chunk 总数: {store.count()}")
    print(f"Gold Chunk 标注: {len(gold_ids_by_case)}/{EXPECTED_RETRIEVAL_CASES} 条已解析")

    # 三组方法共享同一 query 向量：评测比较的是候选/排序差异，不能让同一个
    # query 重复请求三次 Embedding。60 条样本只产生 60 次 Embedding 调用。
    query_vectors: dict[str, list[float]] = {}

    def query_vector(case) -> list[float]:
        if case.case_id not in query_vectors:
            query_vectors[case.case_id] = embed_text(case.query)
        return query_vectors[case.case_id]

    def vector_only_raw(case):
        # 保留旧口径，便于与历史报告直接比较。
        return store.query(query_vector(case), top_k=5, where=_build_where(None)), None

    def vector_only_matched(case):
        # 用和混合链完全相同的向量候选数量、阈值和文档去重规则。
        return retrieve_vector_only(case.query, top_k=5, store=store,
                                    query_vector=query_vector(case)), None

    def hybrid(case):
        stats = RetrievalStats()
        hits = retrieve(case.query, top_k=5, store=store, stats=stats, debug=True,
                        query_vector=query_vector(case))
        return hits, _hybrid_diagnosis(stats, hits, gold_ids_by_case[case.case_id])

    results = [
        _evaluate("vector_only_raw", vector_only_raw, gold_ids_by_case),
        _evaluate("vector_only_matched", vector_only_matched, gold_ids_by_case),
        _evaluate("hybrid", hybrid, gold_ids_by_case),
    ]
    print(f"\n=== RAG Gold Chunk 检索评估（{len(RETRIEVAL_CASES)} 条）===")
    for result in results:
        print(
            f"[{result['name']}] GoldChunk Recall@1={result['gold_chunk_recall@1']} "
            f"Recall@3={result['gold_chunk_recall@3']} "
            f"Recall@5={result['gold_chunk_recall@5']} MRR={result['mrr']}"
        )

    _write_report(report_path, results, collection=store.collection_name)
    print(f"\n报告落盘: {report_path}")


if __name__ == "__main__":
    main()

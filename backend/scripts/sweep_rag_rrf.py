"""RRF 参数网格实验：只评测候选库，不修改在岗索引或 .env。

这不是发布脚本。它会在当前 Python 进程内临时替换 RRF 参数，并对同一
Gold Chunk 检索集比较指标；进程退出前会恢复 Settings。报告路径必须由调用
者明确指定，避免覆盖历史评测报告。

示例（在 backend/ 目录）：
    python -m scripts.sweep_rag_rrf \\
      --collection kb_docs_candidate \\
      --report-path /tmp/rrf_sweep.json
"""
import argparse
import json
from pathlib import Path

from app.config import settings
from app.rag.embedder import embed_text
from app.rag.retriever import retrieve
from app.rag.store import create_store, list_collections
from tests.rag_eval import CASES, _recall_mrr
from tests.rag_eval_dataset import resolve_gold_chunk_ids


def _parse_values(raw: str, parser: argparse.ArgumentParser, *, name: str,
                  value_type, minimum: float) -> list:
    """解析逗号分隔参数，并在 CLI 层拒绝空值、非数字和不安全数值。"""
    try:
        values = [value_type(item.strip()) for item in raw.split(",") if item.strip()]
    except ValueError:
        parser.error(f"{name} 必须是逗号分隔的数字，例如 20,40,60")
    if not values:
        parser.error(f"{name} 至少要提供一个值")
    if any(value < minimum for value in values):
        parser.error(f"{name} 中的每个值都必须 >= {minimum}")
    return list(dict.fromkeys(values))


def _evaluate(store, query_vectors: dict[str, list[float]],
              gold_ids_by_case: dict[str, frozenset[str]]) -> tuple[dict, list[dict]]:
    """复用 rag_eval 的 Gold Chunk 口径，输出汇总和逐例排名。"""
    recall_sums = {k: 0.0 for k in (1, 3, 5)}
    mrr_sum = 0.0
    per_case: list[dict] = []
    for case in CASES:
        hits = retrieve(
            case.query,
            store=store,
            top_k=5,
            query_vector=query_vectors[case.case_id],
        )
        gold_ids = gold_ids_by_case[case.case_id]
        recalls, mrr = _recall_mrr(hits, gold_ids)
        for k, score in recalls.items():
            recall_sums[k] += score
        mrr_sum += mrr
        first_rank = next(
            (rank for rank, hit in enumerate(hits, start=1) if hit.id in gold_ids),
            None,
        )
        per_case.append({
            "case_id": case.case_id,
            "gold_rank": first_rank,
            "recall_at_5": recalls[5],
        })
    total = len(CASES)
    return {
        "cases": total,
        "gold_chunk_recall@1": round(recall_sums[1] / total, 3),
        "gold_chunk_recall@3": round(recall_sums[3] / total, 3),
        "gold_chunk_recall@5": round(recall_sums[5] / total, 3),
        "mrr": round(mrr_sum / total, 3),
    }, per_case


def _result_sort_key(item: dict) -> tuple[float, float, float, float]:
    """优先覆盖率；覆盖相同再比 Top-1 与排序质量。"""
    metrics = item["metrics"]
    return (
        metrics["gold_chunk_recall@5"],
        metrics["gold_chunk_recall@3"],
        metrics["gold_chunk_recall@1"],
        metrics["mrr"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RRF 参数网格实验（只读候选库，不会切换 active）",
    )
    parser.add_argument(
        "--collection",
        default=settings.candidate_collection,
        help="要评测的现有 collection；默认当前候选库",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        required=True,
        help="实验报告 JSON 的明确输出路径（必填，避免覆盖基线）",
    )
    parser.add_argument("--rrf-k-values", default="20,40,60")
    parser.add_argument("--vector-weight-values", default="1.0")
    parser.add_argument("--bm25-weight-values", default="0.5,1.0,1.5")
    parser.add_argument("--recall-n-values", default="10,20")
    parser.add_argument(
        "--fusion-candidate-values", default="0",
        help="RRF 融合后、来源上限前的候选数；0 保持历史 top_k*2",
    )
    args = parser.parse_args()

    if not args.report_path.parent.is_dir():
        parser.error(f"报告目录不存在：{args.report_path.parent}")
    if args.collection not in list_collections(settings.KB_VECTOR_DIR):
        parser.error(f"collection 不存在：{args.collection}")
    if settings.RERANK_ENABLED:
        parser.error("请先关闭 RERANK_ENABLED；本实验只隔离 RRF 候选与融合参数")

    rrf_k_values = _parse_values(
        args.rrf_k_values, parser, name="--rrf-k-values", value_type=float, minimum=1.0,
    )
    vector_weight_values = _parse_values(
        args.vector_weight_values, parser, name="--vector-weight-values",
        value_type=float, minimum=0.0,
    )
    bm25_weight_values = _parse_values(
        args.bm25_weight_values, parser, name="--bm25-weight-values",
        value_type=float, minimum=0.0,
    )
    recall_n_values = _parse_values(
        args.recall_n_values, parser, name="--recall-n-values", value_type=int, minimum=1,
    )
    fusion_candidate_values = _parse_values(
        args.fusion_candidate_values, parser, name="--fusion-candidate-values",
        value_type=int, minimum=0,
    )

    store = create_store(collection_name=args.collection)
    gold_ids_by_case = resolve_gold_chunk_ids(CASES, Path(settings.KB_ROOT))
    print(f"评测 collection: {store.collection_name}（chunks={store.count()}）")
    print("说明：仅在当前进程内试验 RRF；不会修改 .env、知识库或 active 指针。")

    # 参数组合之间复用同一份 60 条 query 向量：避免把 Embedding 的成本和不确定性
    # 混入排序对比。取向量后才开始临时修改 Settings。
    query_vectors = {case.case_id: embed_text(case.query) for case in CASES}
    original = {
        "RRF_K": settings.RRF_K,
        "RRF_VECTOR_WEIGHT": settings.RRF_VECTOR_WEIGHT,
        "RRF_BM25_WEIGHT": settings.RRF_BM25_WEIGHT,
        "RRF_RECALL_N": settings.RRF_RECALL_N,
        "RRF_FUSION_CANDIDATE_N": settings.RRF_FUSION_CANDIDATE_N,
    }
    results: list[dict] = []
    try:
        for fusion_candidates in fusion_candidate_values:
            for recall_n in recall_n_values:
                for rrf_k in rrf_k_values:
                    for vector_weight in vector_weight_values:
                        for bm25_weight in bm25_weight_values:
                            settings.RRF_FUSION_CANDIDATE_N = fusion_candidates
                            settings.RRF_RECALL_N = recall_n
                            settings.RRF_K = rrf_k
                            settings.RRF_VECTOR_WEIGHT = vector_weight
                            settings.RRF_BM25_WEIGHT = bm25_weight
                            metrics, per_case = _evaluate(store, query_vectors, gold_ids_by_case)
                            results.append({
                                "params": {
                                    "fusion_candidates": fusion_candidates,
                                    "rrf_k": rrf_k,
                                    "vector_weight": vector_weight,
                                    "bm25_weight": bm25_weight,
                                    "recall_n": recall_n,
                                },
                                "metrics": metrics,
                                "missed_case_ids": [
                                    row["case_id"] for row in per_case
                                    if row["recall_at_5"] == 0
                                ],
                                "per_case": per_case,
                            })
    finally:
        for name, value in original.items():
            setattr(settings, name, value)

    results.sort(key=_result_sort_key, reverse=True)
    report = {
        "task": "rag_rrf_parameter_sweep",
        "scope": "unfiltered_global_retrieval_candidate_only",
        "collection": store.collection_name,
        "rerank_enabled": False,
        "query_embedding_calls": len(query_vectors),
        "selection_rule": "优先 Gold Chunk Recall@5，其次 Recall@3、Recall@1、MRR",
        "search_space": {
            "rrf_k": rrf_k_values,
            "vector_weight": vector_weight_values,
            "bm25_weight": bm25_weight_values,
            "recall_n": recall_n_values,
            "fusion_candidates": fusion_candidate_values,
        },
        "results": results,
    }
    args.report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    print(f"\n完成 {len(results)} 组组合；按覆盖优先排序的前 5 组：")
    for item in results[:5]:
        params, metrics = item["params"], item["metrics"]
        print(
            f"pool={params['fusion_candidates']} k={params['rrf_k']:g} "
            f"vec={params['vector_weight']:g} "
            f"bm25={params['bm25_weight']:g} n={params['recall_n']} | "
            f"R@1={metrics['gold_chunk_recall@1']:.3f} "
            f"R@3={metrics['gold_chunk_recall@3']:.3f} "
            f"R@5={metrics['gold_chunk_recall@5']:.3f} "
            f"MRR={metrics['mrr']:.3f}"
        )
    print(f"报告落盘: {args.report_path}")


if __name__ == "__main__":
    main()

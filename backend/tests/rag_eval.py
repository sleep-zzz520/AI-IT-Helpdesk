"""RAG 检索与排序评测：以 Gold Chunk 计算 Recall@k 与 MRR。

评测集并非只保存“问题 → 文档”。每条 60 条检索样本均包含标准子 Chunk、
参考答案、是否可回答、问题类型和难度；本脚本只衡量检索/排序层，生成、
澄清和拒答由 ``qa_eval.py`` 使用同一份标注做端到端评测。

用法（backend 目录）：python -m tests.rag_eval --tmp-kb
输出：tests/rag_report.json
"""
import json
from pathlib import Path

from app.rag.embedder import embed_text
from app.rag.retriever import _build_where, retrieve
from app.rag.store import create_store
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


def _evaluate(name: str, search_fn, gold_ids_by_case: dict[str, frozenset[str]]) -> dict:
    """计算 Gold Chunk Recall@k 与第一个 Gold Chunk 的 MRR。"""
    recall_sums = {k: 0.0 for k in (1, 3, 5)}
    mrr_sum = 0.0
    per_case = []
    for case in RETRIEVAL_CASES:
        hits = search_fn(case.query)[:5]
        recalls, mrr = _recall_mrr(hits, gold_ids_by_case[case.case_id])
        for k, score in recalls.items():
            recall_sums[k] += score
        mrr_sum += mrr
        per_case.append({
            "case_id": case.case_id,
            "question_type": case.question_type,
            "difficulty": case.difficulty,
            "gold_chunk_ids": sorted(gold_ids_by_case[case.case_id]),
            "retrieved_chunk_ids": [hit.id for hit in hits],
            "recall_at_5": recalls[5],
        })

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


def main() -> None:
    import argparse
    import tempfile

    from app.config import settings
    from app.rag.store import ChromaStore
    from app.rag.sync import run_sync

    parser = argparse.ArgumentParser()
    parser.add_argument("--tmp-kb", action="store_true", help="用独立临时库重建后评测")
    args = parser.parse_args()

    if args.tmp_kb:
        tmp = Path(tempfile.mkdtemp(prefix="kb_eval_"))
        store = ChromaStore(persist_dir=str(tmp / "chroma"), collection_name="kb_eval")
        report = run_sync(Path(settings.KB_ROOT), store, kb_name="eval_tmp")
        print(f"临时库构建: {report.summary}")
    else:
        store = create_store()

    gold_ids_by_case = resolve_gold_chunk_ids(RETRIEVAL_CASES, Path(settings.KB_ROOT))
    print(f"库内 chunk 总数: {store.count()}")
    print(f"Gold Chunk 标注: {len(gold_ids_by_case)}/{EXPECTED_RETRIEVAL_CASES} 条已解析")

    def vector_only(query: str):
        # 与混合链保持同一“仅子块参与排序”的候选口径；父块只由混合链
        # 在命中后补给生成上下文，不能拿来充当 Gold Chunk 命中。
        return store.query(embed_text(query), top_k=5, where=_build_where(None))

    def hybrid(query: str):
        return retrieve(query, top_k=5, store=store)

    results = [
        _evaluate("vector_only", vector_only, gold_ids_by_case),
        _evaluate("hybrid", hybrid, gold_ids_by_case),
    ]
    print(f"\n=== RAG Gold Chunk 检索评估（{len(RETRIEVAL_CASES)} 条）===")
    for result in results:
        print(
            f"[{result['name']}] GoldChunk Recall@1={result['gold_chunk_recall@1']} "
            f"Recall@3={result['gold_chunk_recall@3']} "
            f"Recall@5={result['gold_chunk_recall@5']} MRR={result['mrr']}"
        )

    REPORT_PATH.write_text(json.dumps({
        "task": "rag_gold_chunk_retrieval",
        "cases": len(RETRIEVAL_CASES),
        "dataset": dataset_summary(RETRIEVAL_CASES),
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告落盘: {REPORT_PATH}")


if __name__ == "__main__":
    main()

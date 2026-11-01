"""影子测试：新旧索引 Recall 对比（蓝绿切换的发布闸门）。

思路（工程上线标准做法——shadow deployment）：
- 同一批 query（复用 tests/rag_eval.py 的 60 条运维用例，带期望命中文档）
- 分别检索【在岗库（active）】与【候选库（candidate）】
- 对比 Recall@1/3/5 + MRR + top-5 文档重叠度
- 结论：候选库指标「不劣于」在岗库 → 才建议切换（零中断发布）

用法：
    cd backend && source ../.venv/bin/activate
    python -m scripts.test_shadow            # 用 rag_eval 用例集
    python -m scripts.test_shadow --top-k 5
"""
import argparse
import json
import time
from pathlib import Path

from app.config import settings
from app.rag.embedder import embed_text
from app.rag.retriever import retrieve
from app.rag.store import create_store
from tests.rag_eval import CASES
from tests.rag_eval_dataset import resolve_gold_chunk_ids


def _hit_sources(hits) -> list[str]:
    """命中的 source_url（去重，保序）。"""
    out = []
    for h in hits:
        src = (h.metadata or {}).get("source_url")
        if src and src not in out:
            out.append(src)
    return out


def _compare_rows(active_rows: list[dict], candidate_rows: list[dict]) -> list[dict]:
    """合并两套逐条结果，供终端展示和 JSON 报告共用。

    影子测试的结论不能只留下“退化 N 条”。每一条差异都要能回看问题、
    Gold 目标、两边排名和命中来源，发布评审时才知道它是否真的影响回答。
    """
    comparisons = []
    for active, candidate in zip(active_rows, candidate_rows, strict=True):
        if active["case_id"] != candidate["case_id"]:
            raise ValueError(
                f"影子测试用例错位：{active['case_id']} != {candidate['case_id']}"
            )
        active_rank = active["hit_pos"] or 99
        candidate_rank = candidate["hit_pos"] or 99
        comparison = (
            "candidate_better" if candidate_rank < active_rank else
            "candidate_worse" if candidate_rank > active_rank else
            "same"
        )
        comparisons.append({
            "case_id": active["case_id"],
            "query": active["query"],
            "expected": active["expected"],
            "comparison": comparison,
            "active": {
                "hit_pos": active["hit_pos"],
                "sources": active["sources"],
            },
            "candidate": {
                "hit_pos": candidate["hit_pos"],
                "sources": candidate["sources"],
            },
        })
    return comparisons


def _evaluate(store, top_k: int, gold_ids_by_case: dict[str, frozenset[str]],
              query_vectors: dict[str, list[float]]) -> tuple[dict, list[dict]]:
    """跑全部用例，返回 (汇总指标, 逐条明细)。"""
    hits_at = {k: 0 for k in (1, 3, 5)}
    mrr_sum = 0.0
    rows = []
    for case in CASES:
        hits = retrieve(case.query, store=store, top_k=top_k,
                        query_vector=query_vectors[case.case_id])[:5]
        gold_ids = gold_ids_by_case[case.case_id]
        hit_idx = next((i for i, h in enumerate(hits)
                        if h.id in gold_ids), None)
        if hit_idx is not None:
            hits_at[1] += hit_idx == 0
            hits_at[3] += hit_idx < 3
            hits_at[5] += hit_idx < 5
            mrr_sum += 1.0 / (hit_idx + 1)
        rows.append({
            "case_id": case.case_id,
            "query": case.query[:30],
            "expected": "/".join(f"{chunk.source_url.split('/')[-1]}#{chunk.section}"
                                 for chunk in case.gold_chunks),
            "hit_pos": hit_idx + 1 if hit_idx is not None else None,
            "sources": _hit_sources(hits)[:3],
        })
    n = len(CASES)
    return {
        "recall_at_1": round(hits_at[1] / n, 3),
        "recall_at_3": round(hits_at[3] / n, 3),
        "recall_at_5": round(hits_at[5] / n, 3),
        "mrr": round(mrr_sum / n, 4),
    }, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="影子测试：新旧索引 Recall 对比")
    parser.add_argument("--top-k", type=int, default=5, help="检索条数（默认 5）")
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "tests" / "shadow_report.json",
        help="影子报告输出路径；默认覆盖 tests/shadow_report.json",
    )
    args = parser.parse_args()
    if not args.report_path.parent.is_dir():
        parser.error(f"报告目录不存在：{args.report_path.parent}")

    active = create_store()  # 在岗库（当前线上）
    candidate = create_store(collection_name=settings.candidate_collection)  # 候选库

    print(f"在岗库   : {settings.active_collection}（chunks={active.count()}）")
    print(f"候选库   : {settings.candidate_collection}（chunks={candidate.count()}）")
    if candidate.count() == 0:
        print("⚠️ 候选库为空：请先同步（POST /api/kb/sync 或 python -m app.rag.sync）")
        return

    gold_ids_by_case = resolve_gold_chunk_ids(CASES, Path(settings.KB_ROOT))
    # 同一 query 在 active/candidate 上只需嵌入一次；影子测试要比较的是索引，
    # 重复 Embedding 只会增加成本，不会增加任何比较信息。
    query_vectors = {case.case_id: embed_text(case.query) for case in CASES}
    t0 = time.perf_counter()
    active_metrics, active_rows = _evaluate(active, args.top_k, gold_ids_by_case, query_vectors)
    cand_metrics, cand_rows = _evaluate(candidate, args.top_k, gold_ids_by_case, query_vectors)
    comparisons = _compare_rows(active_rows, cand_rows)
    elapsed = round((time.perf_counter() - t0) * 1000)

    # ---- 逐条对比表 ----
    print(f"\n=== 逐条对比（top-{args.top_k}，{elapsed}ms）===")
    print(f"{'query':<30}{'期望':<14}{'在岗':<6}{'候选':<6}{'候选 top-3 命中'}")
    for row in comparisons:
        hit_a = f"#{row['active']['hit_pos']}" if row["active"]["hit_pos"] else "—"
        hit_c = f"#{row['candidate']['hit_pos']}" if row["candidate"]["hit_pos"] else "—"
        mark = "✓" if row["comparison"] == "same" else "△ 差异"
        print(f"{row['query']:<30}{row['expected']:<14}{hit_a:<6}{hit_c:<6}"
              f"{row['candidate']['sources']}{mark}")

    # ---- 汇总对比 ----
    print("\n=== 指标对比 ===")
    print(f"{'':<12}{'在岗(active)':<16}{'候选(candidate)':<16}")
    for key, label in (("recall_at_1", "Recall@1"),
                       ("recall_at_3", "Recall@3"),
                       ("recall_at_5", "Recall@5"),
                       ("mrr", "MRR")):
        a, c = active_metrics[key], cand_metrics[key]
        flag = "✓" if c >= a else "⚠️ 退化"
        print(f"{label:<12}{a:<16}{c:<16}{flag}")

    better = sum(row["comparison"] == "candidate_better" for row in comparisons)
    worse = sum(row["comparison"] == "candidate_worse" for row in comparisons)
    print(f"\n候选相对在岗：{better} 条更靠前 / {worse} 条更靠后")
    verdict = "✅ 候选不劣于在岗，可以切换" if worse == 0 else \
              "⚠️ 候选存在退化，建议先排查再切换"
    print(f"结论：{verdict}")

    # 落盘报告（与 rag_report.json 并列，供复盘）
    report = {
        "active": {"collection": settings.active_collection, **active_metrics},
        "candidate": {"collection": settings.candidate_collection, **cand_metrics},
        "better": better, "worse": worse, "verdict": verdict,
        "cases": comparisons,
    }
    args.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告落盘：{args.report_path}")


if __name__ == "__main__":
    main()

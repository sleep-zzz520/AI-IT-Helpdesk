"""Aggregate recorded RAG retrieval, context and operating metrics without model calls."""
import argparse
import json
import math
import re
from pathlib import Path
from statistics import mean


def _ranking(hits, gold, k, complete):
    retrieved = [h["chunk_id"] for h in hits[:k]]
    matches = []
    seen = set()
    for item in retrieved:
        matches.append(item in gold and item not in seen)
        seen.add(item)
    dcg = sum(1 / math.log2(rank + 2) for rank, hit in enumerate(matches) if hit)
    ideal = sum(1 / math.log2(rank + 2) for rank in range(min(k, len(gold))))
    return {
        "recall": sum(matches) / len(gold) if gold else None,
        "hit_rate": int(any(matches)) if gold else None,
        "mrr": next((1 / rank for rank, hit in enumerate(matches, 1) if hit), 0) if gold else None,
        "all_gold": int(gold <= seen) if gold else None,
        "precision": sum(matches) / k if complete else None,
        "relevant_returned": sum(matches) if complete else 0,
        "returned": len(retrieved) if complete else 0,
        "ndcg": dcg / ideal if complete and ideal else None,
    }


def _context_coverage(row, facts):
    context = row.get("injected", [])
    covered = sum(any(
        segment["document_id"] in fact["evidence"]
        and re.search(fact.get("evidence_pattern", fact["pattern"]), segment["text"], re.I)
        for segment in context
    ) for fact in facts)
    return covered, len(facts)


def _aggregate(values):
    valid = [v for v in values if v is not None]
    return {"mean": mean(valid) if valid else None, "scored": len(valid),
            "not_applicable": len(values) - len(valid), "annotated": len(values)}


def _distribution(values, planned):
    values = sorted(values)
    return {"measured": len(values), "planned": planned,
            "mean": mean(values) if values else None,
            **{f"p{p}": values[math.ceil(len(values) * p / 100) - 1] if values else None
               for p in (50, 95, 99)}}


def quality_metrics(report, suite):
    cases = [case for case in suite["cases"] if case["split"] == report["split"]]
    rows = {(r["case_id"], r["mode"]): r for r in report["rows"]}
    ranking = {}
    for k in (1, 3, 5):
        scores = []
        for case in cases:
            expected = case["expected"]
            if "relevant_chunks" not in expected:
                continue
            row = rows.get((case["id"], "retrieval"), {})
            if row.get("errors") != [] or len(row.get("retrieval_calls", [])) != 1:
                hits = []
            else:
                hits = row["retrieval_calls"][0]["hits"]
            scores.append(_ranking(hits, set(expected["relevant_chunks"]), k,
                                   expected.get("relevance_complete") is True))
        ranking[f"@{k}"] = {metric: _aggregate([score[metric] for score in scores])
                            for metric in ("recall", "hit_rate", "mrr", "all_gold", "precision", "ndcg")}
        ranking[f"@{k}"]["returned_hits"] = {
            "relevant": sum(score["relevant_returned"] for score in scores),
            "total": sum(score["returned"] for score in scores),
            "annotated_cases": sum(score["precision"] is not None for score in scores),
        }
    queries, token_totals, seconds = [], [], []
    present = total = 0
    for case in cases:
        row = rows.get((case["id"], "e2e"), {})
        facts = case["expected"]["facts"]
        p, t = _context_coverage(row, facts)
        present += p
        total += t
        if row:
            queries.append(row)
            if "seconds" in row:
                seconds.append(row["seconds"])
            tokens = [call.get("usage", {}).get("total_tokens") for call in row.get("calls", [])]
            if tokens and all(type(n) is int for n in tokens):
                token_totals.append(sum(tokens))
    return {
        "retrieval_chunk": ranking,
        "generation_context_required_facts": {"covered": present, "total": total},
        "operations_e2e": {
            "latency_seconds": _distribution(seconds, len(cases)),
            "tokens_per_query_excludes_indexing": _distribution(token_totals, len(cases)),
            "failed_or_missing": len(cases) - sum(r.get("errors") == [] for r in queries),
            "zero_hit_first_retrieval": sum(
                bool(r.get("retrieval_calls")) and not r["retrieval_calls"][0]["hits"]
                for r in queries
            ),
            "retrieval_observed": sum(bool(r.get("retrieval_calls")) for r in queries),
            "planned": len(cases),
        },
    }


def regressions(report, baseline):
    keys = ("suite_sha256", "snapshot_sha256", "split")
    if any(not report.get(key) or report.get(key) != baseline.get(key) for key in keys):
        raise ValueError("compare the same dataset, snapshot and split")
    return {
        mode: {
            "newly_failed": sorted(set(current["failed_or_missing"])
                                   - set(baseline["summary"][mode]["failed_or_missing"])),
            "fixed": sorted(set(baseline["summary"][mode]["failed_or_missing"])
                            - set(current["failed_or_missing"])),
        }
        for mode, current in report["summary"].items()
    }


def main():
    from scripts.eval_rag_release import digest, load_fixture

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    suite = load_fixture(args.fixture)
    if report["suite_sha256"] != digest(suite):
        parser.error("report and fixture fingerprints differ")
    metrics = quality_metrics(report, suite)
    if args.baseline:
        try:
            metrics["regression"] = regressions(report, json.loads(args.baseline.read_text()))
        except ValueError as exc:
            parser.error(str(exc))
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

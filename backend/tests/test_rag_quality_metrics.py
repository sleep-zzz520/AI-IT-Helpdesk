import pytest

from scripts.rag_quality_metrics import _ranking, quality_metrics, regressions


def test_ranking_deduplicates_and_uses_fixed_k_for_precision():
    hits = [{"chunk_id": item} for item in ("distractor", "g1", "g1")]
    score = _ranking(hits, {"g1", "g2"}, 5, True)
    assert score["recall"] == score["mrr"] == 0.5
    assert score["all_gold"] == 0
    assert score["precision"] == 0.2
    assert 0 < score["ndcg"] < 1
    assert _ranking(hits, {"g1"}, 5, False)["precision"] is None


def test_unanswerable_has_no_recall_but_can_measure_precision():
    score = _ranking([{"chunk_id": "noise"}], set(), 5, True)
    assert score["recall"] is score["mrr"] is score["ndcg"] is None
    assert score["precision"] == 0.0


def test_missing_case_has_zero_recall_and_missing_operation_measurements():
    case = {"id": "a", "split": "frozen", "expected": {
        "relevant_chunks": ["doc:1"], "relevance_complete": True,
        "facts": [{"id": "f", "pattern": "fact", "evidence": ["doc"]}],
    }}
    score = quality_metrics({"split": "frozen", "rows": []}, {"cases": [case]})
    assert score["retrieval_chunk"]["@5"]["recall"]["mean"] == 0.0
    assert score["generation_context_required_facts"] == {"covered": 0, "total": 1}
    assert score["operations_e2e"]["failed_or_missing"] == 1
    assert score["operations_e2e"]["latency_seconds"]["p95"] is None


def test_regression_cannot_compare_two_different_snapshots():
    with pytest.raises(ValueError, match="same dataset"):
        regressions({"suite_sha256": "a"}, {"suite_sha256": "b"})


def test_context_coverage_uses_prompt_even_when_generation_fails():
    case = {"id": "a", "split": "frozen", "expected": {
        "facts": [{"id": "f", "pattern": "fact", "evidence": ["doc"]}],
    }}
    report = {"split": "frozen", "rows": [{
        "case_id": "a", "mode": "e2e", "errors": [{"stage": "generation"}],
        "injected": [{"document_id": "doc", "text": "fact"}],
    }]}
    metrics = quality_metrics(report, {"cases": [case]})
    assert metrics["generation_context_required_facts"] == {"covered": 1, "total": 1}
    assert metrics["operations_e2e"]["failed_or_missing"] == 1

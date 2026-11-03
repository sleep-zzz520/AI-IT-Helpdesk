import pytest

from scripts.eval_rag_release import digest
from scripts.rag_claim_review import score_review


def sample():
    suite = {"cases": [
        {"id": case_id, "split": "frozen", "expected": {"action": "answer"}}
        for case_id in ("a", "b")
    ]}
    row = {"case_id": "a", "mode": "e2e", "answer": "A [证据1]。 B。",
           "injected": [{"text": "A"}], "errors": []}
    report = {"split": "frozen", "suite_sha256": digest(suite), "status": "completed",
              "rows": [row, {**row, "case_id": "b"}]}
    annotation = {"case_id": "a", "answer_relevant": True, "claims_complete": True,
                  "claims": [
                      {"text": "A", "faithful": True, "correct": False,
                       "citations": [{"id": "1", "supports": True}]},
                      {"text": "B", "faithful": False, "correct": False, "citations": []},
                  ]}
    review = {"schema_version": 1, "report_sha256": digest(report),
              "provenance": {"kind": "human", "reviewer": "test", "rubric_version": 1},
              "rows": [annotation, {**annotation, "case_id": "b"}]}
    return report, suite, review


def test_faithfulness_is_independent_of_factual_correctness():
    result = score_review(*sample())
    assert result["human_verified"]
    assert result["metrics"]["faithfulness"] == {"value": 0.5, "cases": 2}
    assert result["metrics"]["factual_correctness"] == {"value": 0.0, "cases": 2}
    assert result["metrics"]["citation_support"] == {"value": 1.0, "cases": 2}


def test_missing_row_keeps_denominator_and_fails_coverage():
    report, suite, review = sample()
    report["rows"].pop()
    review["report_sha256"] = digest(report)
    result = score_review(report, suite, review)
    assert not result["human_verified"]
    assert result["metrics"]["faithfulness"] == {"value": 0.25, "cases": 2}


def test_report_mutation_invalidates_review():
    report, suite, review = sample()
    report["rows"][0]["injected"] = []
    with pytest.raises(ValueError, match="digest mismatch"):
        score_review(report, suite, review)


def test_assistant_review_scores_without_human_gate():
    report, suite, review = sample()
    review["provenance"]["kind"] = "assistant_review"
    result = score_review(report, suite, review)
    assert result["complete"]
    assert not result["human_verified"]


def test_omitted_citation_verdict_fails_coverage():
    report, suite, review = sample()
    review["rows"][0]["claims"][0]["citations"] = []
    result = score_review(report, suite, review)
    assert not result["complete"]
    assert result["reviewed_cases"] == 0


def test_calibration_counts_unsupported_claims_wrongly_accepted():
    from copy import deepcopy

    from scripts.rag_claim_review import calibrate_review

    report, suite, human = sample()
    candidate = deepcopy(human)
    candidate["provenance"]["kind"] = "assistant_review"
    candidate["rows"][0]["claims"][1]["faithful"] = True
    scores = calibrate_review(report, suite, candidate, human)
    assert scores["faithful"] == {"total": 4, "agreement": 2, "false_accepts": 2,
                                  "false_rejects": 0, "human_negative": 2, "human_positive": 2}

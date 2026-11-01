import pytest

from scripts.test_shadow import _compare_rows


def _row(case_id: str, hit_pos: int | None, source: str) -> dict:
    return {
        "case_id": case_id,
        "query": f"问题 {case_id}",
        "expected": "文档#小节",
        "hit_pos": hit_pos,
        "sources": [source],
    }


def test_compare_rows_keeps_publish_review_evidence():
    active = [_row("r01", 1, "active-first"), _row("r02", None, "active-miss"), _row("r03", 2, "active-third")]
    candidate = [_row("r01", 2, "candidate-second"), _row("r02", 1, "candidate-first"), _row("r03", 2, "candidate-third")]

    rows = _compare_rows(active, candidate)

    assert [row["comparison"] for row in rows] == [
        "candidate_worse", "candidate_better", "same",
    ]
    assert rows[0]["case_id"] == "r01"
    assert rows[0]["active"] == {"hit_pos": 1, "sources": ["active-first"]}
    assert rows[0]["candidate"] == {"hit_pos": 2, "sources": ["candidate-second"]}


def test_compare_rows_rejects_misaligned_case_order():
    with pytest.raises(ValueError, match="用例错位"):
        _compare_rows([_row("r01", 1, "active")], [_row("r02", 1, "candidate")])

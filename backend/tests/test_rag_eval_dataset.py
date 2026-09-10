"""RAG 测评集标注自身的回归测试：先验证 Gold，再相信分数。"""
from pathlib import Path

from app.config import BASE_DIR
from tests.rag_eval_dataset import (
    ALL_CASES,
    E2E_CASES,
    EXPECTED_RETRIEVAL_CASES,
    RETRIEVAL_CASES,
    dataset_summary,
    resolve_gold_chunk_ids,
)


def test_every_case_has_required_gold_labels():
    assert len(RETRIEVAL_CASES) == EXPECTED_RETRIEVAL_CASES == 60
    assert len(E2E_CASES) == 22
    assert {"multi_hop", "incomplete", "no_answer", "stale_knowledge_conflict"} <= {
        case.question_type for case in ALL_CASES
    }
    assert all(case.gold_answer and case.question_type and case.difficulty for case in ALL_CASES)
    assert all(case.gold_answer_terms for case in E2E_CASES)


def test_gold_chunk_labels_resolve_against_current_knowledge_documents():
    # conftest 会把 settings.KB_ROOT 换成轻量生命周期夹具；Gold 标注要核对的是
    # 项目正式知识库，不能被该隔离夹具缩小。
    resolved = resolve_gold_chunk_ids(ALL_CASES, Path(BASE_DIR) / "docs" / "knowledge")
    for case in ALL_CASES:
        if case.answerable:
            assert resolved[case.case_id], case.case_id
        else:
            assert not resolved[case.case_id], case.case_id


def test_dataset_summary_exposes_difficulty_and_behavior_composition():
    summary = dataset_summary(ALL_CASES)
    assert summary["difficulties"]["hard"] >= 20
    assert summary["behaviors"] == {"answer": 64, "clarify": 3, "refuse": 3}

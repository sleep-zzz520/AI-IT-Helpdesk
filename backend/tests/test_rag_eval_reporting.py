"""RAG 检索报告的诊断分类测试（离线，无模型调用）。"""

import json

import pytest

from app.rag.retriever import RetrievalStats
from app.rag.store import Hit
from tests.rag_eval import _hybrid_diagnosis, _write_report


def _hit(chunk_id: str) -> Hit:
    return Hit(
        id=chunk_id,
        text="## 测试章节\n内容",
        metadata={"source_url": f"docs/knowledge/{chunk_id}.md"},
        score=0.9,
    )


@pytest.mark.parametrize(
    ("vector", "bm25", "fused", "final", "expected"),
    [
        ([], [], [], [], "not_recalled_by_either_route"),
        ([_hit("gold")], [], [_hit("other")], [_hit("other")],
         "lost_before_rrf_candidate_pool"),
        ([_hit("gold")], [], [_hit("gold")], [_hit("other")],
         "lost_at_final_selection"),
        ([_hit("gold")], [], [_hit("gold")], [_hit("gold")], "final_hit"),
    ],
)
def test_hybrid_diagnosis_identifies_the_stage_that_lost_gold(
    vector, bm25, fused, final, expected,
):
    """同一 Gold 在不同漏失位置，报告必须给出不同、可行动的分类。"""
    stats = RetrievalStats(
        debug_vector_hits=vector,
        debug_bm25_hits=bm25,
        debug_fused_hits=fused,
        debug_final_hits=final,
    )

    diagnosis = _hybrid_diagnosis(stats, final, frozenset({"gold"}))

    assert diagnosis["failure_stage"] == expected
    final_hits = diagnosis["stages"]["final_top_k"]["hits"]
    if final:
        assert final_hits[0]["section"] == "测试章节"
    else:
        assert final_hits == []


def test_write_report_honors_an_explicit_output_path(tmp_path):
    """真实实验可写到独立文件，保留当前基线报告供后续比较。"""
    report_path = tmp_path / "rag_eval_run.json"

    _write_report(report_path, [{"name": "hybrid", "cases": 60}], collection="kb_eval")

    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["collection"] == "kb_eval"
    assert payload["results"] == [
        {"name": "hybrid", "cases": 60}
    ]

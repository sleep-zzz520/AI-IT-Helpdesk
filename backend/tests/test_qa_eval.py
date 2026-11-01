"""端到端评测脚本的离线回归：不调用真实模型或 Embedding。"""

import json
from types import SimpleNamespace

from tests import qa_eval
from tests.rag_eval_dataset import ALL_CASES, E2E_CASES


def test_e2e_set_covers_the_current_retrieval_regressions_and_misses():
    """候选库的排名差异必须进入生成层，不能只停留在 Recall 报告。"""
    case_ids = {case.case_id for case in E2E_CASES}

    assert len(E2E_CASES) == 29
    assert {"r01", "r13", "r19", "r36", "r45", "r57"} <= case_ids
    assert {"r04", "r05", "r10", "r29", "r57"} <= case_ids
    assert all(case.gold_answer_terms for case in E2E_CASES)
    assert len(qa_eval._select_cases("all", "")) == len(ALL_CASES) == 70
    assert all(case.gold_answer_terms for case in qa_eval._select_cases("e2e", ""))


def test_evaluate_writes_a_complete_candidate_report_without_real_model_calls(tmp_path, monkeypatch):
    """每题完成后可落盘，最终报告明确记录 collection 和评测状态。"""
    case = next(item for item in E2E_CASES if item.case_id == "r01")
    gold_id = "docs/knowledge/vpn/cert-renewal.md:2"
    answer = "请执行 certutil -renew，完成后重新拨号。"
    evidence = [{
        "id": gold_id,
        "text": "## 操作命令\ncertutil -renew 后重新拨号。",
        "source": "docs/knowledge/vpn/cert-renewal.md",
    }]
    fake_result = {"answer": answer, "evidence": evidence, "hops": [{"hop": 1}]}
    monkeypatch.setattr(qa_eval, "answer_question", lambda *_args, **_kwargs: fake_result)
    monkeypatch.setattr(
        qa_eval,
        "resolve_gold_chunk_ids",
        lambda *_args, **_kwargs: {"r01": frozenset({gold_id})},
    )
    monkeypatch.setattr(
        qa_eval,
        "faithfulness",
        lambda *_args: {"score": 1.0, "claims": ["续期"], "verdicts": []},
    )
    monkeypatch.setattr(
        qa_eval,
        "answer_relevancy",
        lambda *_args: {"score": 0.9, "questions": ["如何续期"], "similarities": [0.9]},
    )
    store = SimpleNamespace(collection_name="kb_docs_candidate", count=lambda: 80)
    report_path = tmp_path / "candidate_qa.json"

    report = qa_eval.evaluate((case,), store, report_path, sleep_seconds=0)

    assert report["status"] == "completed"
    assert report["collection"] == "kb_docs_candidate"
    assert report["cases_completed"] == 1
    assert report["gold_chunk_coverage_avg"] == 1.0
    assert report["gold_answer_coverage_avg"] == 1.0
    assert report["results"][0]["behavior"]["passed"] is True
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "completed"


def test_behavior_requires_explicit_clarification_and_safe_refusal():
    """关键词出现在泛泛答案中，不能冒充已澄清；拒答不能夹带无证据维修常识。"""
    cases = {case.case_id: case for case in E2E_CASES}

    clarify = qa_eval._behavior_result(
        cases["e03"],
        "可以先排查 VPN。请描述错误码。",
        [],
        {},
        answer_failed=False,
    )
    safe_refusal = qa_eval._behavior_result(
        cases["e06"],
        "知识库中未提供打印机卡纸的相关信息，我可转人工协助。",
        [],
        {},
        answer_failed=False,
    )
    unsafe_refusal = qa_eval._behavior_result(
        cases["e07"],
        "知识库中没有空调信息，可能涉及制冷剂问题，建议您检查滤网后转人工。",
        [],
        {},
        answer_failed=False,
    )

    assert clarify["passed"] is False
    assert set(clarify["missing_terms"]) == {"证书", "设备"}
    assert safe_refusal["passed"] is True
    assert unsafe_refusal["passed"] is False


def test_skip_quality_metrics_and_resume_do_not_repeat_model_calls(tmp_path, monkeypatch):
    """预算模式必须留空未测指标，恢复缓存不能重复花费模型调用。"""
    from unittest.mock import Mock
    case = next(item for item in E2E_CASES if item.case_id == "r01")
    result = {"answer": "certutil -renew 后重新拨号", "evidence": [
        {"id": "gold", "text": "证据", "source": "kb"}], "hops": []}
    answer = Mock(return_value=result)
    quality = Mock(side_effect=AssertionError("quality API must not be called"))
    monkeypatch.setattr(qa_eval, "answer_question", answer)
    monkeypatch.setattr(qa_eval, "faithfulness", quality)
    monkeypatch.setattr(qa_eval, "answer_relevancy", quality)
    monkeypatch.setattr(qa_eval, "resolve_gold_chunk_ids", lambda *_a: {case.case_id: frozenset({"gold"})})
    store = SimpleNamespace(collection_name="isolated", count=lambda: 1)
    report = qa_eval.evaluate((case,), store, tmp_path / "report.json", 0, quality_metrics=False)
    assert report["faithfulness_avg"] is None
    assert report["answer_relevancy_avg"] is None
    assert report["results"][0]["quality_metrics_skipped"] is True
    resumed = qa_eval.evaluate((case,), store, tmp_path / "report.json", 0,
                               quality_metrics=False, resume_results=report["results"])
    assert resumed["cases_completed"] == 1
    assert resumed["resumed_cases"] == 1
    assert answer.call_count == 1
    quality.assert_not_called()


def test_refusal_on_pipeline_failure_is_not_counted_as_success(tmp_path, monkeypatch):
    """免费模型故障导致的固定拒答不可以充当“拒答准确率提升”。"""
    from app.agents.nodes.rag_query import _EMPTY_ANSWER
    case = next(item for item in E2E_CASES if item.expected_behavior == "refuse")
    monkeypatch.setattr(qa_eval, "answer_question", lambda *_a, **_k: {
        "answer": _EMPTY_ANSWER, "evidence": [],
        "hops": [{"judge": {"error": "judge timeout"}}],
    })
    monkeypatch.setattr(qa_eval, "resolve_gold_chunk_ids", lambda *_a: {case.case_id: frozenset()})
    store = SimpleNamespace(collection_name="isolated", count=lambda: 1)
    report = qa_eval.evaluate((case,), store, tmp_path / "report.json", 0, quality_metrics=False)
    assert report["results"][0]["behavior"]["passed"] is None
    assert report["answer_call_error_count"] == 1
    assert report["refusal_accuracy"] is None

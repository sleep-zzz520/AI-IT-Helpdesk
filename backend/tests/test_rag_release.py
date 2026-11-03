import re
from copy import deepcopy

import pytest

from scripts.eval_rag_release import (
    FIXTURE,
    digest,
    load_fixture,
    planned,
    release_errors,
    score_retrieval,
)
from scripts.rag_eval_scoring import score_answer


@pytest.fixture
def suite():
    return load_fixture(FIXTURE)


def test_fixture_examples_support_their_annotated_facts(suite):
    documents = {doc["id"]: doc for doc in suite["documents"]}
    for case in suite["cases"]:
        gold = case["expected"]["required_evidence"]
        if case["expected"]["action"] != "answer":
            continue
        injected = [{"document_id": doc, "text": documents[doc]["text"]} for doc in gold]
        answer = "\n".join(f"{sentence}[证据{i}]"
                           for i, row in enumerate(injected, 1)
                           for sentence in re.split(r"[。；]", row["text"]) if sentence)
        assert score_answer(case, answer, injected)["passed"], case["id"]


def test_expired_conflict_filtered_by_both_retrieval_routes(suite, tmp_path):
    from app.rag.chunks import KnowledgeDoc
    from app.rag.retriever import RetrievalStats, retrieve
    from app.rag.splitter import split_markdown
    from app.rag.store import ChromaStore

    store = ChromaStore(str(tmp_path), "fixture_expiry")
    docs = [doc for doc in suite["documents"] if doc["metadata"]["scenario"] == "vpn"]
    chunks = []
    for doc in docs:
        split = split_markdown(KnowledgeDoc(doc["id"], doc["id"], doc["metadata"], doc["text"]))
        chunks.extend([split.parent, *split.children])
    store.upsert_chunks(chunks, [[1.0, 0.0]] * len(chunks))
    stats = RetrievalStats()
    hits = retrieve("北屿 VPN 证书 BY-41", scenario="vpn", store=store, stats=stats,
                    debug=True, query_vector=[1.0, 0.0])
    for route in (hits, stats.debug_vector_hits, stats.debug_bm25_hits):
        assert {hit.metadata["doc_id"] for hit in route} == {"vpn-check", "vpn-verify"}


@pytest.fixture
def gate_example():
    case = {
        "id": "test", "split": "frozen", "critical": True,
        "expected": {"action": "refuse", "required_evidence": [], "forbidden_evidence": ["denied"]},
    }
    suite = {"cases": [case]}
    answer = "无法提供该信息。"
    report = {"split": "frozen", "suite_sha256": digest(suite), "status": "completed",
              "rows": [
                  {"case_id": "test", "mode": "retrieval", "errors": [],
                   "retrieval_calls": [{"hits": []}], "score": score_retrieval(case, [])},
                  {"case_id": "test", "mode": "e2e", "errors": [], "answer": answer, "injected": [],
                   "score": score_answer(case, answer, [])},
              ]}
    return suite, report


def test_gate_accepts_complete_valid_report(gate_example):
    suite, report = gate_example
    assert not release_errors(report, suite)


@pytest.mark.parametrize("fault", ["omit", "duplicate", "score", "error", "regression", "timeout"])
def test_gate_blocks_incomplete_invalid_or_regressed_report(gate_example, fault):
    suite, report = gate_example
    row = report["rows"][-1]
    if fault == "omit":
        report["rows"].pop()
    elif fault == "duplicate":
        report["rows"].append(deepcopy(row))
    elif fault == "score":
        del row["score"]["facts_supported"]
    elif fault == "error":
        row["errors"].append({"stage": "generation"})
    elif fault == "regression":
        row["answer"] = "请按以下步骤提取。"
    else:
        report["status"] = "budget_exhausted"
    assert release_errors(report, suite)


def test_all_frozen_cases_plan_e2e(suite):
    assert len([case for case, mode in planned(suite, "frozen") if mode == "e2e"]) == 8


def test_employee_policy_catches_cross_scenario_judge_exposure(suite):
    from scripts.eval_rag_release import leaked_evidence

    case = next(c for c, mode in planned(suite, "frozen")
                if c["id"] == "frozen-mail-answer" and mode == "e2e")
    row = {"judge_inputs": [[{"document_id": "print-admin"}]]}
    assert leaked_evidence(case, row) == ["print-admin"]


def test_shared_retrieval_filters_restricted_children_and_parents(tmp_path):
    from app.rag.chunks import Chunk
    from app.rag.retriever import RetrievalStats, retrieve
    from app.rag.store import ChromaStore

    common = {"status": "active", "valid_to": 20991231}
    store = ChromaStore(str(tmp_path), "fixture_access")
    chunks = [
        Chunk("public:1", "门禁故障工单", {
            **common, "doc_id": "public", "chunk_type": "child", "parent_id": "public:parent",
        }),
        Chunk("public:parent", "门禁管理凭据", {
            **common, "doc_id": "public", "chunk_type": "parent", "access": "operator",
        }),
        Chunk("private:1", "门禁故障运维令牌", {
            **common, "doc_id": "private", "chunk_type": "child", "access": "operator",
        }),
    ]
    store.upsert_chunks(chunks, [[1.0, 0.0]] * len(chunks))
    stats = RetrievalStats()
    hits = retrieve("门禁故障", store=store, stats=stats, debug=True, query_vector=[1.0, 0.0])
    assert [hit.id for hit in hits] == ["public:1"]
    assert not hits[0].parent_text
    assert [hit.id for hit in stats.debug_vector_hits] == ["public:1"]
    assert [hit.id for hit in stats.debug_bm25_hits] == ["public:1"]

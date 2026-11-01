"""HTTP management and Agent safety boundaries for published knowledge generations."""
from unittest.mock import Mock

import pytest

from app.agents.nodes import execute, rag_query
from app.rag import generations as g
from app.rag import store as store_module
from app.rag.store import Hit
from tests.test_generations import A, B, snapshot
from tests.test_generations import env as _generation_env


@pytest.fixture(autouse=True)
def mock_llm():
    """These tests explicitly stub only the Agent calls they exercise."""
    yield


@pytest.fixture
def env(request):
    return request.getfixturevalue("_generation_env")


def login(client, username="admin"):
    passwords = {"admin": "Admin@Test123", "zhangsan": "Zhangsan@Test123"}
    response = client.post("/api/auth/login", json={"username": username, "password": passwords[username]})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def judge(enough=True):
    return {"enough": enough, "answerable": enough, "executable": False,
            "missing_info": [] if enough else ["certificate details"],
            "next_query": "" if enough else "VPN alpha certificate"}


def hit(identity=A, source="docs/knowledge/a.md"):
    return Hit(id=f"{identity}:child", text="VPN alpha certificate renewal evidence",
               metadata={"doc_id": identity, "source_url": source}, score=0.9)


def test_admin_sync_job_publish_documents_stats_and_rollback(api_client, api_users, env, monkeypatch):
    headers = login(api_client)
    monkeypatch.setattr(g, "configured_snapshot", snapshot)
    queued = api_client.post("/api/kb/sync", headers=headers)
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["id"]
    assert api_client.get(f"/api/kb/jobs/{job_id}", headers=headers).json()["status"] == "queued"
    assert api_client.post("/api/kb/switch", headers=headers).status_code == 409
    assert g.process_job(job_id)["status"] == "prepared"
    prepared = api_client.get("/api/kb/stats", headers=headers)
    assert prepared.status_code == 200
    assert prepared.json()["candidate_ready"] is True
    assert prepared.json()["active_generation"] is None
    switched = api_client.post("/api/kb/switch", headers=headers)
    assert switched.status_code == 200, switched.text
    assert switched.json()["generation"] == job_id
    stats = api_client.get("/api/kb/stats", headers=headers).json()
    docs = api_client.get("/api/kb/documents", headers=headers).json()
    assert stats["mode"] == "git" and stats["doc_count"] == 2
    assert stats["active_generation"] == job_id and stats["active_chunks"] == 4
    assert docs["total"] == 2 and {item["id"] for item in docs["docs"]} == {A, B}
    assert api_client.get(f"/api/kb/jobs/{job_id}", headers=headers).json()["status"] == "published"
    monkeypatch.setattr(g, "configured_snapshot", lambda: snapshot(2, a="new alpha certificate"))
    queued2 = api_client.post("/api/kb/sync", headers=headers).json()
    assert g.process_job(queued2["id"])["status"] == "prepared"
    assert api_client.post("/api/kb/switch", headers=headers).status_code == 200
    rolled = api_client.post("/api/kb/rollback", headers=headers)
    assert rolled.status_code == 200 and rolled.json()["generation"] == job_id
    assert api_client.get("/api/kb/stats", headers=headers).json()["active_generation"] == job_id


@pytest.mark.parametrize(("method", "path"), [
    ("post", "/api/kb/sync"), ("get", "/api/kb/jobs/nonexistent"), ("post", "/api/kb/rollback"),
])
def test_nonadmin_cannot_manage_generation_jobs(api_client, api_users, env, method, path):
    response = getattr(api_client, method)(path, headers=login(api_client, "zhangsan"))
    assert response.status_code == 403, response.text
    assert env.active() is None and env.calls == []


def test_admin_missing_job_is_404(api_client, api_users, env):
    response = api_client.get("/api/kb/jobs/nonexistent", headers=login(api_client))
    assert response.status_code == 404


def test_generation_query_store_rejects_all_data_mutations(env):
    env.publish()
    store = g.resolve_store()
    before = store.get_all_meta()
    for mutate in [lambda: store.upsert_chunks([], []), lambda: store.delete_ids([]),
                   lambda: store.delete_by_doc_id(A), store.clear]:
        with pytest.raises(RuntimeError, match="查询版本不可"):
            mutate()
    assert store.get_all_meta() == before


def test_answer_hops_keep_original_generation_across_mid_answer_publish(env, monkeypatch):
    first = env.publish()
    old_store = g.resolve_store()
    created, retrieved = [], []

    def create():
        created.append(env.active())
        return g.resolve_store()

    def retrieve(query, scenario=None, top_k=None, store=None):
        retrieved.append(store)
        if len(retrieved) == 1:
            newer = env.publish(snapshot(2, a="new alpha unrelated content"))
            assert newer["id"] != first["id"]
        assert store.collection_name == old_store.collection_name
        return [hit()] if len(retrieved) == 1 else [hit(B, "docs/knowledge/b.md")]

    monkeypatch.setattr(store_module, "create_store", create)
    monkeypatch.setattr(rag_query, "retrieve", retrieve)
    judge_calls = Mock(side_effect=[judge(False), judge(True)])
    monkeypatch.setattr(rag_query, "judge_evidence", judge_calls)
    monkeypatch.setattr(rag_query, "generate_answer", Mock(return_value="verified old generation answer"))
    result = rag_query.answer_question("VPN certificate renewal", "vpn")
    assert len(result["hops"]) == 2 and judge_calls.call_count == 2
    assert result["answer"] == "verified old generation answer"
    assert len(created) == 1 and created == [first["id"]]
    assert len(retrieved) == 2 and retrieved[0] is retrieved[1]


@pytest.mark.parametrize("stage", ["before_judge", "before_generation", "after_generation"])
def test_authoritative_delete_stops_models_or_discards_generated_answer(env, monkeypatch, stage):
    env.publish()
    judge_calls, generation_calls = [], []

    def deny():
        g.enqueue(snapshot(2, a=None))
        assert A in g.denied_ids()

    def retrieve(*args, **kwargs):
        if stage == "before_judge":
            deny()
        return [hit()]

    def decide(*args, **kwargs):
        judge_calls.append(args)
        if stage == "before_generation":
            deny()
        return judge()

    def generate(*args, **kwargs):
        generation_calls.append(args)
        if stage == "after_generation":
            deny()
        return "DO_NOT_RETURN_DELETED_EVIDENCE_ANSWER"

    monkeypatch.setattr(rag_query, "retrieve", retrieve)
    monkeypatch.setattr(rag_query, "judge_evidence", decide)
    monkeypatch.setattr(rag_query, "generate_answer", generate)
    result = rag_query.answer_question("VPN alpha certificate", "vpn")
    assert result["answer"] == rag_query._EMPTY_ANSWER
    assert "DO_NOT_RETURN" not in result["answer"]
    assert len(judge_calls) == (0 if stage == "before_judge" else 1)
    assert len(generation_calls) == (1 if stage == "after_generation" else 0)


@pytest.mark.parametrize("policy_failure", [False, True])
def test_execute_rechecks_deleted_policy_and_never_invokes_action(env, monkeypatch, policy_failure):
    env.publish()
    g.enqueue(snapshot(2, a=None))
    if policy_failure:
        def unavailable():
            raise OSError("policy database unavailable")
        monkeypatch.setattr(g, "denied_ids", unavailable)
    action = Mock(return_value={"success": True})
    monkeypatch.setattr(execute, "execute_action", action)
    result = execute.execute_node({
        "kb_match": {"document_id": A, "action": "vpn.renew_certificate"},
        "actor_id": "zhangsan", "user_id": "zhangsan", "risk_level": "auto",
    })
    assert result["tool_result"]["success"] is False
    assert "转人工" in result["tool_result"]["error"]
    action.assert_not_called()

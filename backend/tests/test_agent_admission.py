"""准入、持久轮次幂等和资源释放；不调用外部模型。"""
import asyncio
import json
import subprocess
import sys
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.config import settings
from app.models import AgentRequest, Conversation
from app.services.agent_admission import (
    acquire_conversation,
    acquire_ingress,
    acquire_slot,
)
from app.services.agent_stream import GuardedStreamingResponse


@pytest.fixture(autouse=True)
def isolate_locks(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(settings, "AGENT_MAX_CONCURRENT", 2)
    monkeypatch.setattr(settings, "AGENT_MAX_INFLIGHT_HTTP", 2)


def _setup(client, monkeypatch):
    from app.api import conversations

    login = client.post("/api/auth/login", json={"username": "zhangsan", "password": "Zhangsan@Test123"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    conv_id = client.post("/api/conversations", headers=headers, json={}).json()["id"]
    calls = []

    def fake_stream(state):
        calls.append(state)
        trace = {"node": "greeting", "result": {"reply": "完成"}}
        yield "node", [trace]
        yield "done", {**state, "trace": [*state.get("trace", []), trace],
                       "messages": state["messages"] + [{"role": "assistant", "content": "完成"}]}, 1

    monkeypatch.setattr(conversations, "_run_agent_stream", fake_stream)
    return conv_id, headers, calls


def test_replay_survives_new_request_without_rerunning_agent(api_client, api_users, db_session, monkeypatch):
    conv_id, headers, calls = _setup(api_client, monkeypatch)
    url = f"/api/conversations/{conv_id}/messages"
    body = {"content": "你好", "request_id": "stable-key-001"}
    first = api_client.post(url, headers=headers, json=body)
    second = api_client.post(url, headers=headers, json=body)
    assert first.status_code == second.status_code == 200
    assert second.headers["x-idempotent-replay"] == "true"
    assert second.text == first.text[first.text.index("event: done"):]
    assert len(calls) == 1
    db_session.expire_all()
    assert len(db_session.get(Conversation, conv_id).messages) == 2
    assert db_session.query(AgentRequest).one().status == "completed"


def test_same_key_different_body_is_rejected(api_client, api_users, monkeypatch):
    conv_id, headers, calls = _setup(api_client, monkeypatch)
    url = f"/api/conversations/{conv_id}/messages"
    api_client.post(url, headers=headers, json={"content": "你好", "request_id": "stable-key-002"})
    result = api_client.post(url, headers=headers, json={"content": "续期", "request_id": "stable-key-002"})
    assert result.status_code == 409
    assert len(calls) == 1


def test_capacity_rejection_has_retry_after_and_does_not_persist_turn(api_client, api_users, db_session, monkeypatch):
    conv_id, headers, calls = _setup(api_client, monkeypatch)
    a, b = acquire_slot(), acquire_slot()
    try:
        result = api_client.post(f"/api/conversations/{conv_id}/messages", headers=headers,
                                 json={"content": "你好", "request_id": "stable-key-003"})
        assert result.status_code == 503
        assert result.headers["retry-after"] == "2"
        assert calls == []
        assert db_session.query(AgentRequest).count() == 0
        assert not db_session.get(Conversation, conv_id).messages
    finally:
        a.release()
        b.release()
    # 被拒绝的请求可以用原键重试。
    assert api_client.post(f"/api/conversations/{conv_id}/messages", headers=headers,
                           json={"content": "你好", "request_id": "stable-key-003"}).status_code == 200


def test_same_conversation_busy_rejects_before_model(api_client, api_users, monkeypatch):
    conv_id, headers, calls = _setup(api_client, monkeypatch)
    lease = acquire_conversation(api_users["zhangsan"].tenant_id, conv_id)
    try:
        result = api_client.post(f"/api/conversations/{conv_id}/messages", headers=headers,
                                 json={"content": "你好"})
        assert result.status_code == 409
        assert not calls
    finally:
        lease.release()


def test_failed_turn_replays_error_without_retrying_tools(api_client, api_users, db_session, monkeypatch):
    from app.api import conversations

    conv_id, headers, calls = _setup(api_client, monkeypatch)

    def fail(state):
        calls.append(state)
        raise RuntimeError("synthetic failure")
        yield

    monkeypatch.setattr(conversations, "_run_agent_stream", fail)
    body = {"content": "续期", "request_id": "stable-key-004"}
    first = api_client.post(f"/api/conversations/{conv_id}/messages", headers=headers, json=body)
    second = api_client.post(f"/api/conversations/{conv_id}/messages", headers=headers, json=body)
    assert "event: error" in first.text
    assert first.text == second.text
    assert len(calls) == 1
    assert db_session.query(AgentRequest).one().status == "failed"


def test_unconfirmed_pending_turn_is_not_automatically_reexecuted(api_client, api_users, db_session, monkeypatch):
    conv_id, headers, calls = _setup(api_client, monkeypatch)
    # 人为插入未确认轮次；不允许靠超时重放可能已经发生的副作用。
    import hashlib

    from app.schemas import MessageCreate
    body = MessageCreate(content="续期", request_id="stable-key-005")
    fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude={"request_id"}), sort_keys=True,
                                            ensure_ascii=False).encode()).hexdigest()
    db_session.add(AgentRequest(conversation_id=conv_id, actor_id="zhangsan", request_id=body.request_id,
                                fingerprint=fingerprint, status="pending"))
    db_session.commit()
    result = api_client.post(f"/api/conversations/{conv_id}/messages", headers=headers, json=body.model_dump())
    assert result.status_code == 409
    assert not calls


def test_file_budget_is_shared_with_another_process():
    lease = acquire_slot()
    second = acquire_slot()
    code = """
from app.config import settings
from app.services.agent_admission import acquire_slot
from fastapi import HTTPException
settings.AGENT_LOCK_DIR = __import__('sys').argv[1]
settings.AGENT_MAX_CONCURRENT = 2
try:
    acquire_slot()
except HTTPException as exc:
    print(exc.status_code)
"""
    try:
        child = subprocess.run([sys.executable, "-c", code, settings.AGENT_LOCK_DIR],
                               text=True, capture_output=True, check=True, timeout=10)
        assert child.stdout.strip() == "503"
    finally:
        lease.release()
        second.release()
    acquire_slot().release()


def test_stream_cleanup_when_transport_fails_before_iteration():
    cleanup = Mock()
    response = GuardedStreamingResponse(iter(["data"]), cleanup, media_type="text/event-stream")

    async def send(_message):
        raise RuntimeError("disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(RuntimeError):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    cleanup.assert_called_once()


def test_ingress_rejects_before_auth_and_leaves_health_available(api_client):
    a, b = acquire_ingress(), acquire_ingress()
    try:
        assert api_client.post("/api/conversations/1/messages", json={"content": "test"}).status_code == 503
        assert api_client.get("/health").status_code == 200
    finally:
        a.release()
        b.release()
    assert api_client.post("/api/conversations/1/messages", json={"content": "test"}).status_code == 401


@pytest.mark.parametrize("identifier", ["%2B1", "-1", "not-an-int"])
def test_ingress_covers_alternative_or_invalid_path_ids(api_client, identifier, monkeypatch):
    from app.main import app
    from app.security import get_current_user

    def forbidden_auth():
        raise AssertionError("overloaded request must not run authentication")

    app.dependency_overrides[get_current_user] = forbidden_auth
    a, b = acquire_ingress(), acquire_ingress()
    try:
        result = api_client.post(f"/api/conversations/{identifier}/messages", json={"content": "test"})
        assert result.status_code == 503
    finally:
        a.release()
        b.release()


def test_auth_returns_detached_identity_and_releases_read_transaction(db_session, api_users):
    from fastapi.security import HTTPAuthorizationCredentials
    from sqlalchemy import inspect

    from app.security import create_token, get_current_user
    user = get_current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=create_token(api_users["zhangsan"].id)), db_session)
    assert user.username == "zhangsan"
    assert inspect(user).detached
    assert not db_session.in_transaction()


def test_large_terminal_uses_mysql_longtext_and_can_replay(api_client, api_users, monkeypatch):
    from sqlalchemy.dialects.mysql import dialect

    from app.api import conversations
    conv_id, headers, calls = _setup(api_client, monkeypatch)

    def large(state):
        calls.append(state)
        yield "done", {**state, "messages": [*state["messages"], {"role": "assistant", "content": "中" * 30000}]}, 1

    monkeypatch.setattr(conversations, "_run_agent_stream", large)
    body = {"content": "large", "request_id": "stable-large-key"}
    first = api_client.post(f"/api/conversations/{conv_id}/messages", headers=headers, json=body)
    second = api_client.post(f"/api/conversations/{conv_id}/messages", headers=headers, json=body)
    assert len(first.content) > 65535
    assert first.text == second.text
    assert len(calls) == 1
    assert AgentRequest.__table__.c.terminal_event.type.compile(dialect=dialect()) == "LONGTEXT"


def test_disconnect_preserves_node_business_state_and_releases_leases(db_session, api_users, monkeypatch):
    from starlette.requests import Request

    from app.api import conversations
    from app.schemas import MessageCreate
    from app.services.session_service import create_conversation
    user = api_users["zhangsan"]
    conv = create_conversation(db_session, user.username, user.tenant_id)

    def node(state):
        trace = {"node": "execute", "result": {"status": "ok"}}
        yield "node", [trace], {**state, "ticket_id": "SAVED-NODE-TICKET", "trace": [trace]}
        raise AssertionError("client disconnected before next node")

    monkeypatch.setattr(conversations, "_run_agent_stream", node)
    response = conversations.send_message(conv.id, MessageCreate(content="test", request_id="disconnect-stable"),
        Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)}), user, db_session)

    async def send(message):
        if message["type"] == "http.response.body":
            raise RuntimeError("disconnected after node")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(RuntimeError):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    db_session.expire_all()
    assert db_session.get(Conversation, conv.id).ticket_id == "SAVED-NODE-TICKET"
    assert db_session.query(AgentRequest).one().status == "interrupted"
    acquire_conversation(user.tenant_id, conv.id).release()
    a, b = acquire_slot(), acquire_slot()
    a.release()
    b.release()

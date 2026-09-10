"""SSE Agent 接口与增量落库测试（离线，内存 SQLite）。"""
from app.models import Conversation


def _login(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "zhangsan", "password": "Zhangsan@Test123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _new_conversation(client):
    response = client.post(
        "/api/conversations",
        headers=_login(client),
        json={},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_sse_emits_node_and_done_and_persists_final_state(
    api_client, api_users, db_session, monkeypatch
):
    import app.api.conversations as conversations_api

    conversation_id = _new_conversation(api_client)
    received_state = {}

    def fake_stream(state):
        received_state.update(state)
        yield "node", [{"node": "intent", "result": {"intent": "vpn"}}]
        final = {
            **state,
            "intent": "vpn",
            "messages": state["messages"] + [
                {"role": "assistant", "content": "测试执行完成"},
            ],
            "trace": [
                {"node": "intent", "result": {"intent": "vpn"}},
                {"node": "close", "result": {"ticket_id": "TKT-SSE-1"}},
            ],
            "ticket_id": "TKT-SSE-1",
            "tool_result": {"status": "ok"},
        }
        yield "done", final, 37

    monkeypatch.setattr(conversations_api, "_run_agent_stream", fake_stream)
    response = api_client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=_login(api_client),
        json={"content": "VPN 连不上"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: node" in response.text
    assert "event: done" in response.text
    assert "测试执行完成" in response.text
    assert received_state["actor_id"] == "zhangsan"
    assert received_state["tenant_id"] == api_users["zhangsan"].tenant_id
    assert received_state["execution_source"] == "web_agent"
    assert received_state["operation_id"].startswith("web-agent:")

    db_session.expire_all()
    conversation = db_session.get(Conversation, conversation_id)
    assert conversation.status == "resolved"
    assert conversation.ticket_id == "TKT-SSE-1"
    assert [m.role for m in conversation.messages] == ["user", "assistant"]
    assert conversation.messages[-1].elapsed_ms == 37
    assert [t.node for t in conversation.traces] == ["intent", "close"]
    assert [(log.from_status, log.to_status) for log in conversation.status_logs] == [
        ("new", "processing"),
        ("processing", "resolved"),
    ]


def test_sse_error_persists_user_message_and_emits_error(
    api_client, api_users, db_session, monkeypatch
):
    import app.api.conversations as conversations_api

    conversation_id = _new_conversation(api_client)

    def broken_stream(_state):
        if False:
            yield "unreachable", []
        raise RuntimeError("Agent 节点崩溃")

    monkeypatch.setattr(conversations_api, "_run_agent_stream", broken_stream)
    response = api_client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=_login(api_client),
        json={"content": "这轮执行会失败"},
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "Agent 执行失败（RuntimeError）" in response.text
    assert "event: done" not in response.text

    db_session.expire_all()
    conversation = db_session.get(Conversation, conversation_id)
    assert conversation.status == "processing"
    assert [m.content for m in conversation.messages] == ["这轮执行会失败"]

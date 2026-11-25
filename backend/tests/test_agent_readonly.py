"""API regression for explicit read-only requests."""
import json

import pytest

from app.models import AuditLog, ExecutionOperation


def _done_payload(response) -> dict:
    """从 SSE 流中取终态 done 事件的数据。"""
    marker = "event: done\ndata: "
    assert marker in response.text, response.text
    return json.loads(response.text.split(marker, 1)[1].split("\n\n", 1)[0])


@pytest.mark.parametrize("reuse_prior_intent", [False, True])
def test_explicit_no_renewal_routes_read_only(
    api_client, api_users, db_session, mock_llm, monkeypatch, reuse_prior_intent,
):
    """当前用户消息的执行限制优先于模型的默认故障处理分类。"""
    from app.agents.nodes import rag_query

    monkeypatch.setattr(rag_query, "retrieve", lambda *args, **kwargs: [])
    login = api_client.post(
        "/api/auth/login",
        json={"username": "zhangsan", "password": "Zhangsan@Test123"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    created = api_client.post("/api/conversations", headers=headers, json={})
    assert created.status_code == 200

    path = f"/api/conversations/{created.json()['id']}/messages"
    previous_traces = []
    if reuse_prior_intent:
        mock_llm["extract"] = {"error_code": "800"}
        first = api_client.post(path, headers=headers, json={"content": "VPN 报错 800，帮我排查。"})
        previous_traces = _done_payload(first)["traces"]
        assert previous_traces[-1]["node"] == "ask"

    response = api_client.post(
        path, headers=headers,
        json={"content": "我的 Windows 11 VPN 报错 800。不要续期，只想了解能不能代同事 lisi 办理。"},
    )

    assert response.status_code == 200
    payload = _done_payload(response)
    assert [trace["node"] for trace in payload["traces"][len(previous_traces):]] == ["read_only", "rag_query"]
    assert payload["conversation"]["status"] == "processing"
    assert db_session.query(ExecutionOperation).count() == 0
    assert db_session.query(AuditLog).filter_by(action="execute_vpn_renew_certificate").count() == 0

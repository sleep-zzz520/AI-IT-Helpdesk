"""用户自定义模型配置的安全与调用链回归测试。"""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.models import LLMConfig


@pytest.fixture()
def llm_encryption_key(monkeypatch):
    """测试使用一次性 Fernet key，避免依赖开发机 .env。"""
    from app.config import settings

    monkeypatch.setattr(settings, "LLM_CONFIG_ENCRYPTION_KEY", Fernet.generate_key().decode())


def _auth(client, username="zhangsan", password="Zhangsan@Test123"):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _body(**overrides):
    return {
        "name": "我的 GPT",
        "base_url": "https://api.example.com/v1/",
        "model": "example-chat",
        "api_key": "sk-test-private-key",
        "json_mode": True,
        **overrides,
    }


def _create(client, **overrides):
    response = client.post("/api/llm-configs", headers=_auth(client), json=_body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def test_model_config_encrypts_key_and_never_returns_it(
    api_client, api_users, db_session, llm_encryption_key
):
    from app.services.llm_config_service import profile_from_config

    created = _create(api_client)

    assert created["base_url"] == "https://api.example.com/v1"
    assert created["has_api_key"] is True
    assert "api_key" not in created
    assert "api_key_ciphertext" not in created

    stored = db_session.get(LLMConfig, created["id"])
    assert stored.owner_user_id == api_users["zhangsan"].id
    assert stored.api_key_ciphertext != "sk-test-private-key"
    assert "sk-test-private-key" not in stored.api_key_ciphertext
    assert profile_from_config(stored).api_key == "sk-test-private-key"

    listed = api_client.get("/api/llm-configs", headers=_auth(api_client))
    assert listed.status_code == 200
    assert listed.json() == [created]


def test_model_config_is_private_and_cannot_be_used_by_another_user(
    api_client, api_users, db_session, llm_encryption_key
):
    created = _create(api_client)
    lisi_headers = _auth(api_client, "lisi", "Lisi@Test123")

    listed = api_client.get("/api/llm-configs", headers=lisi_headers)
    assert listed.status_code == 200
    assert listed.json() == []

    lisi_conversation = api_client.post("/api/conversations", headers=lisi_headers, json={})
    assert lisi_conversation.status_code == 200
    response = api_client.post(
        f"/api/conversations/{lisi_conversation.json()['id']}/messages",
        headers=lisi_headers,
        json={"content": "测试越权模型", "llm_config_id": created["id"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "模型配置不存在"


def test_selected_model_profile_reaches_agent_without_persisting_secret(
    api_client, api_users, db_session, llm_encryption_key, monkeypatch
):
    import app.api.conversations as conversations_api

    created = _create(api_client)
    conversation = api_client.post("/api/conversations", headers=_auth(api_client), json={})
    assert conversation.status_code == 200
    received = {}

    def fake_stream(state):
        received["profile"] = state["llm_profile"]
        final = {
            **state,
            "messages": state["messages"] + [{"role": "assistant", "content": "已使用自定义模型"}],
            "trace": [{"node": "intent", "result": {"ok": True}}],
        }
        yield "done", final, 1

    monkeypatch.setattr(conversations_api, "_run_agent_stream", fake_stream)
    response = api_client.post(
        f"/api/conversations/{conversation.json()['id']}/messages",
        headers=_auth(api_client),
        json={"content": "VPN 连不上", "llm_config_id": created["id"]},
    )

    assert response.status_code == 200
    assert received["profile"].config_id == created["id"]
    assert received["profile"].model == "example-chat"
    assert received["profile"].api_key == "sk-test-private-key"
    assert "sk-test-private-key" not in response.text

    stored = db_session.get(LLMConfig, created["id"])
    assert "sk-test-private-key" not in stored.api_key_ciphertext


def test_connection_test_does_not_persist_key_or_return_it(
    api_client, api_users, db_session, llm_encryption_key, monkeypatch
):
    import app.api.llm_configs as llm_configs_api

    monkeypatch.setattr(llm_configs_api, "test_connection", lambda _profile: 42)
    response = api_client.post("/api/llm-configs/test", headers=_auth(api_client), json=_body())

    assert response.status_code == 200
    assert response.json() == {"ok": True, "latency_ms": 42}
    assert db_session.query(LLMConfig).count() == 0
    assert "sk-test-private-key" not in response.text


def test_agent_llm_nodes_forward_selected_profile(monkeypatch):
    """用户选择必须真正影响意图、抽取与 RAG 生成，不只是停在 API state。"""
    from app.agents.nodes import extract, intent, rag_query
    from app.services.llm_config_service import profile_from_values

    profile = profile_from_values(
        name="我的 GPT",
        base_url="https://api.example.com/v1",
        model="example-chat",
        api_key="sk-test-private-key",
        json_mode=True,
    )
    seen_profiles = []

    def fake_chat_json(messages, **kwargs):
        seen_profiles.append(kwargs.get("llm_profile"))
        prompt = messages[0]["content"]
        if "意图分类器" in prompt:
            return {"intent": "vpn", "request_type": "troubleshoot"}
        return {"device": "Windows 11", "error_code": "800", "username": ""}

    monkeypatch.setattr(intent, "chat_json", fake_chat_json)
    monkeypatch.setattr(extract, "chat_json", fake_chat_json)
    state = {"messages": [{"role": "user", "content": "VPN Error 800，Windows 11"}], "llm_profile": profile}
    intent.intent_node(state)
    extract.extract_node(state)

    hit = SimpleNamespace(
        id="vpn-guide",
        text="VPN 证书续期说明",
        parent_text=None,
        score=0.9,
        metadata={"source_url": "docs/knowledge/vpn/cert-renewal.md"},
        routes={"vector"},
    )
    monkeypatch.setattr(rag_query, "retrieve", lambda *_args, **_kwargs: [hit])

    def fake_judge(_query, _evidence, incoming_profile):
        seen_profiles.append(incoming_profile)
        return {"enough": True, "answerable": True, "next_query": ""}

    def fake_answer(_query, _evidence, _judge, incoming_profile):
        seen_profiles.append(incoming_profile)
        return "自定义模型回答"

    monkeypatch.setattr(rag_query, "judge_evidence", fake_judge)
    monkeypatch.setattr(rag_query, "generate_answer", fake_answer)
    result = rag_query.answer_question("VPN 怎么续期", llm_profile=profile)

    assert result["answer"] == "自定义模型回答"
    assert seen_profiles == [profile, profile, profile, profile]


def test_deleting_config_removes_only_owners_configuration(
    api_client, api_users, db_session, llm_encryption_key
):
    created = _create(api_client)
    response = api_client.delete(f"/api/llm-configs/{created['id']}", headers=_auth(api_client))

    assert response.status_code == 204
    assert db_session.get(LLMConfig, created["id"]) is None

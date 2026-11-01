"""项目默认文本模型路由：必须固定 glm-5.3-flash，不能静默回退。"""
from types import SimpleNamespace

from app.config import settings


def _response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_project_default_text_chains_are_locked_to_glm_5_3_flash():
    """聊天、结构化任务和旧 fast 字段都不能引入其他默认回答模型。"""
    assert settings.GLM_MODELS == ["glm-5.3-flash"]
    assert settings.GLM_MODELS_FAST == ["glm-5.3-flash"]
    assert settings.GLM_JSON_MODELS == ["glm-5.3-flash"]


def test_chat_and_chat_json_use_the_locked_default_chain(monkeypatch):
    """两个 LLM 入口都将同一条单模型链交给底层调用器。"""
    from app import llm

    observed = []

    def fake_create(chain, **kwargs):
        observed.append((list(chain), kwargs))
        content = '{"ok": true}' if "response_format" in kwargs else "普通回答"
        return _response(content), chain[0]

    monkeypatch.setattr(llm, "_create", fake_create)

    assert llm.chat([{"role": "user", "content": "测试"}]) == "普通回答"
    assert llm.chat_json([{"role": "user", "content": "测试"}]) == {"ok": True}
    assert [chain for chain, _kwargs in observed] == [
        ["glm-5.3-flash"],
        ["glm-5.3-flash"],
    ]

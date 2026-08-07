"""寒暄边界测试：只有"纯寒暄"才走 greeting，实质内容降级咨询。

背景（会话 175 实测 bug）：用户问"比亚迪"（知识库语料话题），LLM 判
intent=other + request_type=other → 路由 greeting → 答非所问。
修复：规则层 _is_pure_greeting 判定——去掉寒暄/礼貌词后无实质内容才算
纯寒暄；有实质内容 → request_type 降级 consult → rag_query 全库检索
（多库联合下语料库可命中）。

覆盖：
- 纯寒暄词表（你好/在吗/谢谢/你好吗/hello…）→ greeting
- 实质内容（知识库话题词/天气/帮我看看…）→ rag_query（不 greeting）
"""
from types import SimpleNamespace

import pytest

from app.agents.graph import graph
from app.agents.nodes.intent import _is_pure_greeting


# ===== 规则层：纯寒暄判定 =====
@pytest.mark.parametrize("msg,expect_pure", [
    # 纯寒暄（无实质内容）
    ("你好", True),
    ("您好，在吗？", True),
    ("hello", True),
    ("谢谢", True),
    ("你好吗", True),
    ("在吗", True),
    # 有实质内容（不是寒暄）
    ("比亚迪", False),          # 知识库语料话题词（会话 175）
    ("今天天气怎么样", False),  # 无关但非寒暄 → 降级咨询知识库兜底
    ("你好，帮我看看", False),  # 问候 + 实质请求
])
def test_is_pure_greeting_rule(msg, expect_pure):
    assert _is_pure_greeting(msg) is expect_pure, f"判定错误: {msg}"


# ===== 集成：路由行为 =====
@pytest.fixture()
def mock_retrieve(monkeypatch):
    """mock rag_query 的 retrieve：避免测试里真实调用 embedding API。"""
    import app.agents.nodes.rag_query as m

    def fake_retrieve(query, scenario=None, top_k=5):
        return []  # 0 命中 → rag_query 内部兜底话术（验证路由即可）

    monkeypatch.setattr(m, "retrieve", fake_retrieve)
    return fake_retrieve


def _invoke(mock_llm, msg: str) -> list[str]:
    mock_llm["intent"] = {"intent": "other", "request_type": "other", "reason": "mock"}
    out = graph.invoke({
        "messages": [{"role": "user", "content": msg}],
        "user_id": "zhangsan",
        "trace": [],
    })
    return [t["node"] for t in out.get("trace", [])]


def test_topic_word_goes_rag_query_not_greeting(mock_llm, mock_retrieve):
    """LLM 误判 other/other + 消息有实质内容 → 降级咨询走 rag_query。"""
    nodes = _invoke(mock_llm, "比亚迪")
    assert "greeting" not in nodes, f"话题词不应走寒暄: {nodes}"
    assert "rag_query" in nodes, f"应降级咨询走知识库: {nodes}"


def test_pure_greeting_goes_greeting(mock_llm):
    """纯寒暄 → greeting 友好回复（不转人工、不检索）。"""
    nodes = _invoke(mock_llm, "你好")
    assert "greeting" in nodes, f"纯寒暄应走 greeting: {nodes}"
    assert "rag_query" not in nodes, f"纯寒暄不应检索: {nodes}"


def test_unrelated_word_goes_rag_query(mock_llm, mock_retrieve):
    """无关话题（非寒暄）→ 也降级咨询（知识库兜底，不答非所问）。"""
    nodes = _invoke(mock_llm, "今天天气怎么样")
    assert "greeting" not in nodes
    assert "rag_query" in nodes

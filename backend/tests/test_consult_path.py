"""咨询问答路径测试（离线，mock LLM + mock 检索）。

覆盖 ROADMAP P3.1：咨询诉求（consult）走 rag_query 问答，不触发执行。
- mock LLM：conftest autouse（intent 判定 consult + rag_query 的 judge/generate）
- mock 检索：patch rag_query 模块的 retrieve，返回确定性命中
   （不依赖真实 RAG 向量库 + MySQL 台账）

验证：consult 诉求 → rag_query 节点 → 回答来自 mock 证据；无 verify/execute。
"""
from types import SimpleNamespace

import pytest

from app.agents.graph import graph


@pytest.fixture()
def mock_retrieve(monkeypatch):
    """mock rag_query 的 retrieve：返回确定性知识命中。"""
    import app.agents.nodes.rag_query as m

    def fake_retrieve(query, scenario=None, top_k=5):
        hit = SimpleNamespace(
            id="doc-1",
            text="VPN 客户端配置步骤：1. 下载客户端 2. 输入服务器地址",
            parent_text="VPN 客户端配置步骤：1. 下载客户端 2. 输入服务器地址 3. 连接测试",
            score=0.9,
            metadata={"source_url": "docs/knowledge/vpn/client-config.md"},
            routes={"vector", "bm25"},  # 检索路标（Hit.routes，judge 降级判定用）
        )
        return [hit]

    monkeypatch.setattr(m, "retrieve", fake_retrieve)
    return fake_retrieve


def _invoke_consult(mock_llm, user_msg: str) -> dict:
    # 意图判定为 consult（咨询诉求）→ 走 rag_query 问答路径
    mock_llm["intent"] = {"intent": "vpn", "request_type": "consult", "reason": "mock"}
    return graph.invoke({
        "messages": [{"role": "user", "content": user_msg}],
        "user_id": "zhangsan",
        "trace": [],
    })


def _nodes(out: dict) -> list[str]:
    return [t["node"] for t in out.get("trace", [])]


def test_consult_goes_rag_query_no_execute(mock_llm, mock_retrieve):
    """咨询诉求：走 rag_query，不触发执行链路。"""
    out = _invoke_consult(mock_llm, "VPN客户端怎么配置")
    nodes = _nodes(out)

    assert "rag_query" in nodes, f"咨询应走问答路径: {nodes}"
    assert "verify" not in nodes, "咨询不应查证执行"
    assert "execute" not in nodes, "咨询不应触发执行"
    assert "close" not in nodes, "咨询不关工单"


def test_consult_answer_from_evidence(mock_llm, mock_retrieve):
    """回答基于 mock 证据生成（mock chat 返回），且 trace 带检索来源。"""
    out = _invoke_consult(mock_llm, "VPN客户端怎么配置")
    reply = out["messages"][-1]["content"]

    # mock chat 返回固定回答
    assert "知识库" in reply or "答案" in reply, f"回答应来自知识问答: {reply}"

    rq_trace = next(t for t in out.get("trace", []) if t["node"] == "rag_query")
    assert rq_trace["result"].get("sources") == ["docs/knowledge/vpn/client-config.md"], \
        "trace 应记录检索来源"


def test_consult_does_not_persist_ticket(mock_llm, mock_retrieve):
    """咨询纯只读：不产生工单号（不关单）。"""
    out = _invoke_consult(mock_llm, "VPN客户端怎么配置")
    assert not out.get("ticket_id"), "咨询问答不应产生工单"

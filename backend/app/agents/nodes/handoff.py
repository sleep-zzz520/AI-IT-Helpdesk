"""转人工节点：高风险 / 未命中方案的兜底出口。

真实系统里这里会：创建人工工单、通知 L1 工程师。
MVP 先给用户一句明确的转人工回复。
"""
from app.agents.state import HelpdeskState


def handoff_node(state: HelpdeskState) -> dict:
    kb = state.get("kb_match", {})
    if kb.get("matched"):
        reason = f"方案「{kb.get('solution')}」风险等级为 {kb.get('risk')}，需人工审批"
    else:
        reason = f"知识库未匹配到合适方案（{kb.get('reason')}）"

    reply = f"⚠️ {reason}。您的工单已转人工处理，请留意后续通知。"
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "trace": state["trace"] + [{"node": "handoff", "result": {"reply": reply}}],
    }

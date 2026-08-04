"""转人工节点：三种情况的兜底出口（真实系统里会开人工工单、通知 L1）。

1. 高风险方案需审批
2. 知识库未命中
3. 执行 Tool 失败
"""
from app.agents.state import HelpdeskState


def handoff_node(state: HelpdeskState) -> dict:
    kb = state.get("kb_match", {})
    tr = state.get("tool_result", {})

    if state.get("error"):
        # 系统异常兜底（LLM 彻底失败等）——最优先，与业务原因区分
        reason = f"系统处理异常：{state['error']}"
    elif tr.get("status") == "error":
        reason = f"操作执行失败：{tr.get('reason')}"
    elif kb.get("matched"):
        reason = f"方案「{kb.get('solution')}」风险等级为 {kb.get('risk')}，需人工审批"
    else:
        reason = f"知识库未匹配到合适方案（{kb.get('reason')}）"

    reply = f"⚠️ {reason}。您的工单已转人工处理，请留意后续通知。"
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "trace": [{"node": "handoff", "result": {"reply": reply}}],
    }

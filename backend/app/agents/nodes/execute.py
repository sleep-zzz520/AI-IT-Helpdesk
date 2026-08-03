"""⑥执行节点：调用执行类 Tool（只有过风险分级 auto 才会到这里）。

输入：kb_match.action（工具名）+ user_id（执行对象）。
输出：tool_result（成功/失败），失败由条件边兜底转人工。
"""
from app.agents.state import HelpdeskState
from app.tools.actions import execute_action


def execute_node(state: HelpdeskState) -> dict:
    action = state.get("kb_match", {}).get("action", "")
    user_id = state.get("user_id", "")
    result = execute_action(action, user_id)
    return {
        "tool_result": result,
        "trace": state["trace"] + [{
            "node": "execute",
            "result": {"action": action, "user_id": user_id, "tool_result": result},
        }],
    }

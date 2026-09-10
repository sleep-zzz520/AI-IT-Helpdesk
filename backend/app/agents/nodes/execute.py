"""⑥执行节点：调用执行类 Tool（只有过风险分级 auto 才会到这里）。

输入：kb_match.action + 入口注入的可信身份上下文。
输出：tool_result（成功/失败），失败由条件边兜底转人工。
"""
from app.agents.state import HelpdeskState
from app.services.vpn_execution_service import ExecutionRequest
from app.tools.actions import execute_action


def execute_node(state: HelpdeskState) -> dict:
    action = state.get("kb_match", {}).get("action", "")
    request = ExecutionRequest(
        # actor_id 来自入口认证；target_user_id 来自会话归属。服务会再校验二者相同。
        actor_id=state.get("actor_id", ""),
        tenant_id=state.get("tenant_id"),
        target_user_id=state.get("user_id", ""),
        risk_decision=state.get("risk_level", ""),
        source=state.get("execution_source", ""),
        operation_id=state.get("operation_id", ""),
    )
    result = execute_action(action, request)
    return {
        "tool_result": result,
        "trace": [{
            "node": "execute",
            "result": {
                "action": action,
                "actor_id": request.actor_id,
                "target_user_id": request.target_user_id,
                "operation_id": request.operation_id,
                "tool_result": result,
            },
        }],
    }


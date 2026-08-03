"""③查证节点：按场景配置的 verify_tool，从查询工具注册表调用。

输入：user_id（会话身份）+ intent（决定用哪个查证工具）。
输出：cert_status（Tool 返回的原始结果，原样存进 state 供路由/审计用）。
"""
from app.agents.scenarios import SCENARIOS
from app.agents.state import HelpdeskState
from app.tools.monitor_api import QUERY_REGISTRY


def verify_node(state: HelpdeskState) -> dict:
    scenario = SCENARIOS.get(state.get("intent"), {})
    tool_name = scenario.get("verify_tool")
    fn = QUERY_REGISTRY.get(tool_name)
    # user_id 来自会话上下文（登录态/工单系统），不是问出来的
    user_id = state.get("user_id") or "zhangsan"

    if fn is None:
        result = {"status": "error", "reason": f"场景未配置可用的查证工具: {tool_name}"}
    else:
        result = fn(user_id)

    return {
        "cert_status": result,
        "trace": state["trace"] + [{
            "node": "verify",
            "result": {"verify_tool": tool_name, "user_id": user_id, "cert_status": result},
        }],
    }

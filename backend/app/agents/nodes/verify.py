"""③查证节点：调用监控 API（Tool）查询证书状态。

输入：user_id（会话身份，系统上下文自带）。
输出：cert_status（Tool 返回的原始结果，原样存进 state 供后面路由/审计用）。
"""
from app.agents.state import HelpdeskState
from app.tools.monitor_api import check_cert_status


def verify_node(state: HelpdeskState) -> dict:
    # 查谁的证书？user_id 来自会话上下文（登录态/工单系统），不是问出来的
    user_id = state.get("user_id") or "zhangsan"
    result = check_cert_status(user_id)
    return {
        "cert_status": result,
        "trace": state["trace"] + [{
            "node": "verify",
            "result": {"user_id": user_id, "cert_status": result},
        }],
    }

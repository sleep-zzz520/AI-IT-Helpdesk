"""执行类 Tool（有副作用）：续期证书、重建拨号连接等。

【注册表模式】——MCP 思想的雏形：
知识库给的是工具【名字】，这里维护"名字 → 函数"的映射。
加新工具 = 注册表加一行，节点代码零改动。
"""
from app.tools.monitor_api import MOCK_USERS


def _renew_certificate(user_id: str) -> dict:
    """mock 续期：模拟执行 certutil -renew + 重新拨号。"""
    if user_id == "error_user":  # 演示执行失败的账号
        return {"status": "error", "reason": "证书服务当前不可达，续期失败"}
    # 模拟续期成功：把证书有效期更新到明年
    MOCK_USERS[user_id]["cert_valid_until"] = "2027-01-15"
    MOCK_USERS[user_id]["expired"] = False
    return {"status": "ok", "message": f"已为用户 {user_id} 自动续期证书，有效期至 2027-01-15"}


def _rebuild_connection(user_id: str) -> dict:
    """mock 重建拨号连接。"""
    return {"status": "ok", "message": f"已重建用户 {user_id} 的 VPN 拨号连接"}


# 工具注册表：名字 → 函数
TOOL_REGISTRY = {
    "vpn.renew_certificate": _renew_certificate,
    "vpn.rebuild_connection": _rebuild_connection,
}


def execute_action(action: str, user_id: str) -> dict:
    """按名字调用工具。返回：
    - 成功: {"status": "ok", "message": str}
    - 失败: {"status": "error", "reason": str}
    """
    fn = TOOL_REGISTRY.get(action)
    if fn is None:
        return {"status": "error", "reason": f"未注册的工具: {action}"}
    return fn(user_id)

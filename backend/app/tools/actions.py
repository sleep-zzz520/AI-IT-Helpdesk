"""执行类 Tool（有副作用）：续期证书、重建拨号连接等。

【注册表模式】——MCP 思想的雏形：
知识库给的是工具【名字】，这里维护"名字 → 函数"的映射。
加新工具 = 注册表加一行，节点代码零改动。
"""
from app.tools.monitor_api import renew_certificate


def _renew_certificate(user_id: str) -> dict:
    """续期证书：调 monitor_api（mock/real 双模式自动切换）。

    原来直接改 MOCK_USERS 字典，现在收敛到 monitor_api.renew_certificate，
    real 模式走 HTTP POST 到监控服务的续期接口。
    本函数只是注册表里的一个薄封装，保持 TOOL_REGISTRY 模式不变。
    """
    return renew_certificate(user_id)


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

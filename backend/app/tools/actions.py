"""执行类 Tool 的兼容注册表。

【注册表模式】——MCP 思想的雏形：
知识库给的是工具【名字】，这里维护"名字 → 函数"的映射。
加新工具 = 注册表加一行，节点代码零改动。
"""
from collections.abc import Callable

from app.services.vpn_execution_service import ExecutionRequest, renew_vpn_certificate


def _renew_certificate(request: ExecutionRequest) -> dict:
    """续期必须进入共享领域服务，不能让注册表直接越过安全边界。"""
    return renew_vpn_certificate(request)


def _rebuild_connection(request: ExecutionRequest) -> dict:
    """mock 重建拨号连接。"""
    return {"status": "ok", "message": f"已重建用户 {request.target_user_id} 的 VPN 拨号连接"}


# 工具注册表：名字 → 函数
TOOL_REGISTRY: dict[str, Callable[[ExecutionRequest], dict]] = {
    "vpn.renew_certificate": _renew_certificate,
    "vpn.rebuild_connection": _rebuild_connection,
}


def execute_action(action: str, request: ExecutionRequest) -> dict:
    """按名字调用工具。返回：
    - 成功: {"status": "ok", "message": str}
    - 失败: {"status": "error", "reason": str}
    """
    fn = TOOL_REGISTRY.get(action)
    if fn is None:
        return {"status": "error", "reason": f"未注册的工具: {action}"}
    return fn(request)

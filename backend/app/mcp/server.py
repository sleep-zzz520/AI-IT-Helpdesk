"""智能 IT 运维服务台的本地 stdio MCP Server。

Phase MCP-1 只发布无副作用的能力：
- Tool：查询本地演示主体的 VPN 证书状态；
- Resource：读取已纳入知识库的 VPN 证书续期 SOP。

这已经是完整 MCP 协议 Server：外部 Host 可通过 stdio 发现和调用
Tool / Resource。写操作会在后续阶段接入统一的身份、风险、幂等和审计服务，
不能直接暴露 `renew_certificate()`。
"""
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app.config import BASE_DIR, settings
from app.tools.monitor_api import check_cert_status

MCP_RESOURCE_URI = "it-helpdesk://sop/vpn/certificate-renewal"
VPN_CERT_RENEWAL_SOP_PATH = BASE_DIR / "docs" / "knowledge" / "vpn" / "cert-renewal.md"

mcp = MCPServer(
    "ai-it-helpdesk",
    instructions=(
        "这是智能 IT 运维服务台的本地只读 MCP Server。"
        "可查询本地演示主体的 VPN 证书状态，或读取 VPN 证书续期 SOP。"
        "它不提供直接执行续期的工具。"
    ),
)


def _local_demo_principal() -> str:
    """返回本地 stdio 演示主体；空配置时拒绝调用而不猜测身份。"""
    principal = settings.MCP_LOCAL_DEMO_USER_ID
    if not principal:
        raise ToolError("MCP_LOCAL_DEMO_USER_ID 未配置，无法确定本地演示主体")
    return principal


@mcp.tool(
    title="查询我的 VPN 证书状态",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def get_my_vpn_certificate_status() -> dict[str, object]:
    """查询本地演示主体的 VPN 证书状态，不执行任何修改操作。"""
    principal = _local_demo_principal()
    return {
        "principal": f"local-demo:{principal}",
        **check_cert_status(principal),
    }


@mcp.resource(
    MCP_RESOURCE_URI,
    mime_type="text/markdown",
    title="VPN 证书续期 SOP",
)
def get_vpn_certificate_renewal_sop() -> str:
    """读取知识库中的 VPN 证书续期 SOP，供 Host 按需加载为上下文。"""
    try:
        return Path(VPN_CERT_RENEWAL_SOP_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"无法读取 VPN 证书续期 SOP: {exc}") from exc


if __name__ == "__main__":
    # 默认 transport=stdio：Host 启动此进程后，经 stdin/stdout 进行 MCP 通信。
    # 绝不能往 stdout 写普通日志，否则会破坏 JSON-RPC 消息流。
    mcp.run()

"""MCP Server 的协议契约测试（离线、无端口、无子进程）。"""
import pytest
from mcp import Client

from app.mcp import server


@pytest.fixture
def anyio_backend():
    """项目只验证 asyncio；避免 anyio 因安装 trio 而重复执行。"""
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_server_advertises_read_only_tool_and_sop_resource():
    """Client 能发现 Tool/Resource，才说明不是进程内注册表伪装成 MCP。"""
    async with Client(server.mcp, raise_exceptions=True) as client:
        tools = await client.list_tools()
        tool = next(item for item in tools.tools if item.name == "get_my_vpn_certificate_status")

        assert tool.annotations.read_only_hint is True
        assert tool.input_schema["properties"] == {}

        resources = await client.list_resources()
        resource = next(item for item in resources.resources if item.uri == server.MCP_RESOURCE_URI)

        assert resource.mime_type == "text/markdown"
        assert resource.name == "get_vpn_certificate_renewal_sop"


@pytest.mark.anyio
async def test_mcp_tool_calls_existing_monitor_contract(monkeypatch):
    """MCP Tool 复用 monitor_api 契约，不复制查证业务逻辑。"""
    monkeypatch.setattr(
        server,
        "check_cert_status",
        lambda username: {
            "status": "ok",
            "expired": username == "zhangsan",
            "cert_valid_until": "2026-07-30",
        },
    )

    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.call_tool("get_my_vpn_certificate_status", {})

    assert result.is_error is False
    assert result.structured_content == {
        "principal": "local-demo:zhangsan",
        "status": "ok",
        "expired": True,
        "cert_valid_until": "2026-07-30",
    }


@pytest.mark.anyio
async def test_mcp_resource_reads_the_existing_vpn_sop():
    """Resource 必须来自项目现有知识库文档，而不是重复维护一份 MCP 文案。"""
    async with Client(server.mcp, raise_exceptions=True) as client:
        result = await client.read_resource(server.MCP_RESOURCE_URI)

    content = result.contents[0]
    assert content.mime_type == "text/markdown"
    assert "# VPN 证书续期 SOP" in content.text
    assert "Error 800" in content.text

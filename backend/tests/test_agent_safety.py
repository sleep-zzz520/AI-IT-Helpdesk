"""Agent 安全边界与异常兜底测试（离线）。

测试重点不是模型能否生成答案，而是：
- LLM/节点异常时必须落到 handoff，不把异常暴露成 500
- 执行 Tool 失败时不能误关单
- 信息不足时必须追问，不得凭空执行
- 多问题只做引导，不误触发单一场景执行链
- 未知风险和未知工具默认向安全侧收敛
"""
from app.agents.graph import graph
from app.agents.nodes import parallel
from app.agents.nodes.risk import risk_node
from app.agents.nodes.safe import safe
from app.services.vpn_execution_service import ExecutionRequest
from app.tools.actions import execute_action


def _nodes(state: dict) -> list[str]:
    return [item["node"] for item in state.get("trace", [])]


def _invoke(message: str) -> dict:
    return graph.invoke({
        "messages": [{"role": "user", "content": message}],
        "user_id": "zhangsan",
        "trace": [],
    })


def test_safe_converts_node_exception_to_error_trace():
    def broken(_state):
        raise RuntimeError("模型服务不可用")

    result = safe(broken)({"messages": [], "trace": []})

    assert result["error"] == "RuntimeError: 模型服务不可用"
    assert result["trace"] == [{
        "node": "error",
        "result": {"error": "模型服务不可用"},
    }]


def test_llm_exception_routes_to_handoff(monkeypatch):
    def broken(_state):
        raise RuntimeError("GLM 限流")

    # parallel_round_node 持有的是模块级函数引用，必须 patch 这个引用，
    # 不能只 patch app.agents.nodes.intent.chat_json。
    monkeypatch.setattr(parallel, "intent_node", broken)
    result = _invoke("VPN 连不上")

    assert _nodes(result)[-1] == "handoff"
    assert result["error"] == "RuntimeError: GLM 限流"
    assert "系统处理异常" in result["messages"][-1]["content"]


def test_missing_fields_asks_instead_of_executing(mock_llm):
    mock_llm["extract"] = {"device": "", "error_code": "", "username": ""}
    result = _invoke("VPN 连不上")

    assert _nodes(result)[-1] == "ask"
    assert result["missing_info"] == ["device", "error_code"]
    assert "设备型号" in result["messages"][-1]["content"]
    assert "错误代码" in result["messages"][-1]["content"]
    assert "execute" not in _nodes(result)


def test_tool_failure_routes_to_handoff_without_close(monkeypatch):
    import app.agents.nodes.execute as execute_node_module

    monkeypatch.setattr(
        execute_node_module,
        "execute_action",
        lambda _action, _user_id: {
            "status": "error",
            "reason": "监控服务不可达",
        },
    )
    result = _invoke("VPN 连不上，报错 Error 800，设备是 Windows 11")

    assert "execute" in _nodes(result)
    assert _nodes(result)[-1] == "handoff"
    assert "close" not in _nodes(result)
    assert "监控服务不可达" in result["messages"][-1]["content"]


def test_multiple_scenarios_are_guided_without_execution():
    result = _invoke("VPN 连不上，密码也忘记了")

    assert _nodes(result)[-1] == "multi"
    assert "VPN" in result["messages"][-1]["content"]
    assert "密码" in result["messages"][-1]["content"]
    assert not set(_nodes(result)) & {"check", "verify", "execute", "close"}


def test_unknown_risk_defaults_to_human():
    result = risk_node({
        "kb_match": {
            "matched": True,
            "solution": "未知方案",
            "risk": "not-a-policy-value",
        },
        "trace": [],
    })

    assert result["risk_level"] == "human"


def test_unknown_tool_returns_error_contract():
    result = execute_action("tool.does_not_exist", ExecutionRequest(
        actor_id="zhangsan",
        tenant_id=1,
        target_user_id="zhangsan",
        risk_decision="auto",
        source="web_agent",
        operation_id="unknown-tool-test",
    ))

    assert result == {
        "status": "error",
        "reason": "未注册的工具: tool.does_not_exist",
    }

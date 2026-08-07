"""Agent 故障执行链路回归测试（离线，mock LLM + mock 监控）。

覆盖 ROADMAP P3.1「mock LLM / 内存 DB：回归不依赖真实 GLM 调用与 MySQL」：
- 不调真实 GLM（conftest autouse mock_llm patch 全部调用点）
- 不连 MySQL（graph.invoke 走 mock 监控，工单沉淀在 state，不落库）

被测对象：LangGraph 全图。输入一句话 → 意图识别 → 抽取 → 查证 → 知识库
→ 风险分级 → 执行/兜底。验证的是【编排正确性】和【确定性分支路由】。
"""
from app.agents.graph import graph


def _invoke(user_msg: str, user_id: str = "zhangsan") -> dict:
    return graph.invoke({
        "messages": [{"role": "user", "content": user_msg}],
        "user_id": user_id,
        "trace": [],
    })


def _nodes(out: dict) -> list[str]:
    return [t["node"] for t in out.get("trace", [])]


def test_vpn_full_auto_close(mock_llm):
    """低风险（800）：全自动闭环，走到 close，含 execute。"""
    out = _invoke("VPN连不上，报错Error 800，设备是Windows 11")
    nodes = _nodes(out)

    assert "intent" in nodes and "extract" in nodes, f"缺感知节点: {nodes}"
    assert "verify" in nodes, f"缺查证节点: {nodes}"
    assert out.get("cert_status", {}).get("expired"), "证书应判定过期"
    assert "execute" in nodes, f"低风险应自动执行: {nodes}"
    assert nodes[-1] == "close", f"应走到关单收尾，实际: {nodes}"
    assert out.get("ticket_id"), "应有工单号"
    assert out.get("risk_level") == "auto", "低风险应为 auto"


def _invoke_with_code(mock_llm, error_code: str, user_id: str = "zhangsan") -> dict:
    """定制 extract 返回指定错误码（mock LLM 默认写死 800，无法区分分支）。"""
    mock_llm["extract"] = {"device": "Windows 11", "error_code": error_code, "username": ""}
    return _invoke(f"VPN连不上，报错Error {error_code}，设备是Windows 11", user_id=user_id)


def test_vpn_medium_risk_handoff(mock_llm):
    """中风险（720 → rebuild）：转人工，不自动执行。"""
    out = _invoke_with_code(mock_llm, "720")
    nodes = _nodes(out)

    assert "risk" in nodes, f"缺风险分级节点: {nodes}"
    assert out.get("risk_level") == "human", f"中风险应转人工，实际: {out.get('risk_level')}"
    assert "handoff" in nodes and nodes[-1] == "handoff", f"应走到转人工: {nodes}"
    assert "execute" not in nodes, "中风险不得自动执行"


def test_vpn_unknown_error_handoff(mock_llm):
    """未知错误码（999）：kb 未命中 → 兜底转人工。"""
    out = _invoke_with_code(mock_llm, "999")
    nodes = _nodes(out)

    assert nodes[-1] == "handoff", f"未命中应兜底转人工: {nodes}"
    assert "execute" not in nodes, "未命中不得执行"


def test_cert_valid_no_execute(mock_llm):
    """证书正常（lisi）：不执行，直接收尾回复。"""
    out = _invoke("VPN连不上，帮我看看", user_id="lisi")  # lisi 证书正常（mock）
    nodes = _nodes(out)

    assert "verify" in nodes, f"缺查证: {nodes}"
    assert not out.get("cert_status", {}).get("expired"), "lisi 证书应正常"
    assert "execute" not in nodes, "证书正常不执行"
    assert "finalize" in nodes, f"应走正常收尾: {nodes}"


def test_reused_intent_saves_llm_call(mock_llm):
    """多轮补全：第二轮意图复用（会话级），不重复判意图。"""
    out1 = _invoke("VPN连不上，报错800")
    assert "intent" in _nodes(out1)

    # 第二轮：带历史 + 追问回答，意图应复用（intent 节点走 reuse 分支，不调 LLM）
    out2 = graph.invoke({
        "messages": out1["messages"] + [{"role": "user", "content": "Windows 11"}],
        "intent": out1.get("intent"),
        "request_type": out1.get("request_type"),
        "user_id": "zhangsan",
        "trace": out1.get("trace", []),
    })
    intent_trace = [t for t in out2.get("trace", []) if t["node"] == "intent"]
    assert any(t["result"].get("reused") == "vpn" for t in intent_trace), \
        "第二轮意图应复用（reused=vpn），不重新调 LLM"

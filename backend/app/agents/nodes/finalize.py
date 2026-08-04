"""流程结束节点：非支持场景 / 查询失败 / 状态正常的收尾回复。

话术全部来自场景注册表（SCENARIOS）：
- 非 active 场景（other / coming）：用 reply_unsupported（配置里配了就有）
- active 场景：用 replies 模板（vpn 是证书分支）
"""
from app.agents.scenarios import SCENARIOS
from app.agents.state import HelpdeskState


def finalize_node(state: HelpdeskState) -> dict:
    intent = state.get("intent")
    scenario = SCENARIOS.get(intent)

    if scenario is None or scenario.get("status") != "active":
        # 未实现/不支持的场景：场景专属话术，绝不套用其他场景的业务话术
        reply = scenario.get("reply_unsupported", "当前服务台正在试运行，您的问题已转人工处理。") \
            if scenario else "当前服务台正在试运行，您的问题已转人工处理。"
    else:
        replies = scenario["replies"]
        cs = state.get("cert_status", {})
        if cs.get("status") == "error":
            reply = replies["error"].format(reason=cs.get("reason"))
        else:
            reply = replies["valid"].format(date=cs.get("cert_valid_until"))

    return {
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "trace": [{"node": "finalize", "result": {"reply": reply}}],
    }

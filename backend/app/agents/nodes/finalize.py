"""流程结束节点：不支持意图 / 查询失败 / 证书正常的收尾回复。

（证书过期会走④知识库匹配继续处理，不经过这里）
"""
from app.agents.state import HelpdeskState


def finalize_node(state: HelpdeskState) -> dict:
    intent = state.get("intent")

    # 非支持场景（如寒暄"你好"）：告知范围并转人工，绝不误触业务流程
    if intent == "other":
        reply = (
            "当前服务台支持 VPN 连接故障与密码问题。"
            "您描述的问题不在支持范围，已记录并转人工处理。"
        )
    else:
        cs = state.get("cert_status", {})
        if cs.get("status") == "error":
            reply = f"⚠️ 查询失败：{cs.get('reason')}。请稍后重试，或转人工客服处理。"
        else:
            reply = (
                f"✅ 您的证书状态正常（有效期至 {cs.get('cert_valid_until')}）。"
                "VPN 连不上可能另有原因，建议检查网络，或联系人工排查。"
            )

    return {
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "trace": state["trace"] + [{"node": "finalize", "result": {"reply": reply}}],
    }

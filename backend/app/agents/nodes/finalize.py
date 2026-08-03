"""流程结束节点：非支持意图 / 查询失败 / 证书正常的收尾回复。

（证书过期会走④知识库匹配继续处理，不经过这里）
"""
from app.agents.state import HelpdeskState


def finalize_node(state: HelpdeskState) -> dict:
    intent = state.get("intent")

    # 当前 MVP 只实现 VPN 场景：非 vpn 意图（other/password）统一转人工，
    # 绝不套用证书话术（修复：密码问题曾回复"证书状态正常（有效期至 None）"）
    if intent != "vpn":
        reply = (
            "当前服务台正在试运行 VPN 连接故障的自动处理。"
            "您的问题已记录并转人工处理，请留意后续通知。"
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

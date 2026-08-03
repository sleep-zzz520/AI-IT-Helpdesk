"""流程结束节点：查询失败 / 证书正常的收尾回复。

（证书过期会走④知识库匹配继续处理，不经过这里）
"""
from app.agents.state import HelpdeskState


def finalize_node(state: HelpdeskState) -> dict:
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

"""寒暄节点：request_type=other（"你好"等）的友好回复。

为什么需要（Phase 5 优化）：之前寒暄走 finalize 的 reply_unsupported
默认话术——"您的问题已转人工处理"，打个招呼就被转人工，不合理。
寒暄 = 问候 + 能力介绍，不触发任何流程、不产生工单。
"""
from app.agents.scenarios import SCENARIOS
from app.agents.state import HelpdeskState

# 能力介绍从场景注册表动态生成（加场景自动出现在问候里）
_ACTIVE_NAMES = "、".join(sc["name"] for sc in SCENARIOS.values())

GREETING = (f"你好！我是 IT 服务台智能助手 🤖\n"
            f"我可以帮你处理：{_ACTIVE_NAMES}。\n"
            f"请描述你的问题（支持文字或截图），比如「VPN 连不上，报错 800」。")


def greeting_node(state: HelpdeskState) -> dict:
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": GREETING}],
        "trace": [{"node": "greeting", "result": {"reply": GREETING}}],
    }

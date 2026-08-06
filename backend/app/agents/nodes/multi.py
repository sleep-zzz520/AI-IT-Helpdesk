"""多问题引导节点：用户一条消息描述多个场景（如 "vpn和密码都连不上"）。

为什么需要这个节点（Phase 5 优化）：
- intent 是单一值，LLM 对多问题消息只能取一个场景，另一个诉求被忽略
- 之前的兜底是转人工——不合理：用户只是"一次说了两件事"
- 引导用户逐个描述（真实客服同样做法），会话保持 open，零转人工

触发：intent 节点规则检测到 ≥2 个场景关键词（_detect_multi）→ multi_scenarios。
"""
from app.agents.scenarios import SCENARIOS
from app.agents.state import HelpdeskState

# 场景 key → 展示名（引导话术用）
_NAMES = {k: sc["name"] for k, sc in SCENARIOS.items()}

REPLY_TEMPLATE = ("我检测到您同时描述了多个问题（{scenarios}）。"
                  "为了准确处理，请先描述其中一个问题，我会逐个帮您解决。")


def multi_node(state: HelpdeskState) -> dict:
    names = [_NAMES.get(k, k) for k in state.get("multi_scenarios", [])]
    reply = REPLY_TEMPLATE.format(scenarios="、".join(names))
    return {
        # 消费即清除：多问题是一次性引导，下一轮用户聚焦单问题时
        # 路由不能再命中 multi（否则永远走不到正常流程）
        "multi_scenarios": None,
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "trace": [{"node": "multi",
                   "result": {"scenarios": state.get("multi_scenarios", []),
                              "reply": reply}}],
    }

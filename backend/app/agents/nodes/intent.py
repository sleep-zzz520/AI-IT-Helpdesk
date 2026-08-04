"""意图识别节点：流程的「闸门」。

职责：判断用户描述属于哪个场景（或 other）。
提示词从 SCENARIOS 注册表动态生成——加场景自动生效，不用改 prompt。
"""
from app.agents.scenarios import SCENARIOS
from app.agents.state import HelpdeskState
from app.llm import chat_json

_INTENT_NAMES = " / ".join(list(SCENARIOS) + ["other"])


def _build_intent_prompt() -> str:
    """从场景注册表生成分类规则（加场景 = 自动出现在这里）。"""
    parts = ["你是 IT 运维服务台的意图分类器。判断用户问题属于哪一类："]
    for key, sc in SCENARIOS.items():
        parts.append(f"\n【{sc['name']}】典型描述：{' / '.join(sc['keywords'])}")
    parts.append("\n【其他】不属于以上任何一类（寒暄、询问概念、其他故障等）。")
    parts.append(f'\n只输出 JSON，格式：{{"intent": "{_INTENT_NAMES}", "reason": "一句话理由"}}')
    return "\n".join(parts)


INTENT_PROMPT = _build_intent_prompt()


def intent_node(state: HelpdeskState) -> dict:
    """输入：最新一条用户消息；输出：intent 分类 + Trace 记录。

    意图是【会话级】判断：一旦定下，后续轮次直接复用，不再重复调 AI。
    """
    if state.get("intent"):
        return {"trace": [{
            "node": "intent",
            "result": {"reused": state["intent"]},
        }]}

    user_msg = state["messages"][-1]["content"]

    reply = chat_json([
        {"role": "system", "content": INTENT_PROMPT},
        {"role": "user", "content": user_msg},
    ])

    intent = reply.get("intent", "other")
    if intent not in list(SCENARIOS) + ["other"]:
        intent = "other"
    return {
        "intent": intent,
        "trace": [{
            "node": "intent",
            "result": reply,
        }],
    }

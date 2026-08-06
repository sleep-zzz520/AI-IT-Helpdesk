"""意图识别节点：流程的「闸门」。

职责：判断用户描述属于哪个场景（intent）+ 诉求类型（request_type）。
提示词从 SCENARIOS 注册表动态生成——加场景自动生效，不用改 prompt。

Phase 3 升级为二维分类：
- intent：走哪条业务流（vpn/password/email/software/other）
- request_type：走执行流程还是问答路径（troubleshoot/consult/other）
一次 LLM 调用同时输出两个维度（与 extract 并行），零额外调用、零额外延迟。
"""
from app.agents.scenarios import SCENARIOS
from app.agents.state import HelpdeskState
from app.llm import chat_json

_INTENT_NAMES = " / ".join(list(SCENARIOS) + ["other"])
_REQUEST_TYPES = "troubleshoot / consult / other"


def _build_intent_prompt() -> str:
    """从场景注册表生成分类规则（加场景 = 自动出现在这里）。"""
    parts = ["你是 IT 运维服务台的意图分类器。判断用户问题的【场景】和【诉求类型】："]
    for key, sc in SCENARIOS.items():
        parts.append(f"\n【{sc['name']}】典型描述：{' / '.join(sc['keywords'])}")
    parts.append("\n【其他】不属于以上任何一类（寒暄、与 IT 无关等）。")
    parts.append(f"""
【诉求类型】判断标准：有无明确的故障现象（报错/连不上/失败/异常）
- troubleshoot 故障诉求：存在明确故障现象，需要排查修复或执行操作（报错、连不上、坏了）
- consult 咨询诉求：询问操作方法/流程/概念，无明确故障现象（怎么办、怎么配、流程是什么）
- other 其他：寒暄、无关话题（如"你好"）

只输出 JSON：{{"intent": "{_INTENT_NAMES}", "request_type": "{_REQUEST_TYPES}", "reason": "一句话理由"}}""")
    return "\n".join(parts)


INTENT_PROMPT = _build_intent_prompt()


def intent_node(state: HelpdeskState) -> dict:
    """输入：最新一条用户消息；输出：intent 分类 + request_type + Trace 记录。

    意图与诉求类型都是【会话级】判断：一旦定下，后续轮次直接复用，
    不再重复调 AI（与历史行为一致，request_type 随 intent 一并复用）。
    """
    if state.get("intent"):
        return {"trace": [{
            "node": "intent",
            "result": {"reused": state["intent"],
                       "request_type": state.get("request_type", "troubleshoot")},
        }]}

    user_msg = state["messages"][-1]["content"]

    reply = chat_json([
        {"role": "system", "content": INTENT_PROMPT},
        {"role": "user", "content": user_msg},
    ], model_chain=state.get("model_chain"))

    intent = reply.get("intent", "other")
    if intent not in list(SCENARIOS) + ["other"]:
        intent = "other"
    request_type = reply.get("request_type", "troubleshoot")
    if request_type not in ("troubleshoot", "consult", "other"):
        request_type = "troubleshoot"  # 非法值归一化：默认故障（宁多问不漏报障）
    return {
        "intent": intent,
        "request_type": request_type,
        "trace": [{
            "node": "intent",
            "result": reply,
        }],
    }

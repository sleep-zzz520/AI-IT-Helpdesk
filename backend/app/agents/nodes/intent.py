"""意图识别节点：流程的「闸门」。

职责：判断用户描述属于哪个场景（intent）+ 诉求类型（request_type）。
提示词从 SCENARIOS 注册表动态生成——加场景自动生效，不用改 prompt。

Phase 3 升级为二维分类：
- intent：走哪条业务流（vpn/password/email/software/other）
- request_type：走执行流程还是问答路径（troubleshoot/consult/other）
一次 LLM 调用同时输出两个维度（与 extract 并行），零额外调用、零额外延迟。

Phase 5 加拼写纠错兜底：LLM 判 other 时，规则层用编辑距离找近似关键词
（bpn→vpn），避免打字错误直接转人工（详见 _fuzzy_fix_intent）。
"""
import re

from app.agents.scenarios import SCENARIOS
from app.agents.state import HelpdeskState
from app.llm import chat_json

_INTENT_NAMES = " / ".join([*list(SCENARIOS), "other"])
_REQUEST_TYPES = "troubleshoot / consult / other"


def _levenshtein(a: str, b: str) -> int:
    """编辑距离（DP）。关键词都很短（<10 字符），成本可忽略。"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# 消息中的「词」（拉丁字母/数字串，中文无空格不分词）
_WORD_RE = re.compile(r"[a-z0-9]+")


def _kw_near(msg: str, w: str) -> str | None:
    """消息中是否有关键词 w 的近似词（编辑距离 ≤1），返回命中的词。

    - 精确包含：直接命中（LLM 误判 other 但消息里就有关键词，中英文皆可）
    - 近似：只对【ASCII 词】按词边界匹配——打字错误发生在英文/数字词上
      （bpn→vpn 替换 / vpvn→vpn 插入 / vn→vpn 删除）
    - 为什么不做中文近似：中文无空格，"连不上" 与 "装不上" 恰巧距离 1
      但语义无关（踩过：vpn 命中被判歧义吞掉）。中文只走精确匹配。
    """
    if w in msg:
        return w
    if not w.isascii() or not any(c.isalpha() for c in w):
        return None  # 非 ASCII 或纯数字（800）：只精确，不模糊
    for word in _WORD_RE.findall(msg):
        if _levenshtein(word, w) <= 1:
            return word
    return None


def _fuzzy_fix_intent(user_msg: str, llm_intent: str) -> dict | None:
    """拼写纠错兜底：LLM 判 other 时，规则层找场景关键词的近似词。

    动机：用户把 vpn 打成 bpn，LLM 会判"非支持场景"→ 直接转人工，
    不合理（打字错误 ≠ 业务不受支持）。编辑距离 ≤1 是"肉眼可识别"
    的打字错误界限；且只修正"唯一命中"（多场景都近邻 = 歧义，不修）。
    返回 {"intent": key, "correction": {...}} 或 None（保持 other）。
    """
    msg = user_msg.lower()
    best: list[tuple[str, str]] = []  # (scenario, 命中的词/近似词描述)
    for key, sc in SCENARIOS.items():
        for kw in sc["keywords"]:
            hit = _kw_near(msg, kw.lower())
            if hit:
                best.append((key, kw if hit == kw.lower() else f"{kw}≈{hit}"))
    scenes = {k for k, _ in best}
    if len(scenes) != 1:
        return None  # 0 命中（真·寒暄/无关）或歧义（多个场景都近邻）
    key = scenes.pop()
    return {
        "intent": key,
        "correction": {
            "from": llm_intent,
            "to": key,
            "matched": next(m for k, m in best if k == key),
        },
    }


def _build_intent_prompt() -> str:
    """从场景注册表生成分类规则（加场景 = 自动出现在这里）。"""
    parts = ["你是 IT 运维服务台的意图分类器。判断用户问题的【场景】和【诉求类型】："]
    for sc in SCENARIOS.values():
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

    意图复用的【边界】——这是踩坑修复（见优化文档）：
    - 具体场景（vpn/password/email/software）复用：多轮补全信息时（"VPN连不上"
      →"win11"）意图不该变，复用省一次 LLM 调用
    - other（寒暄/无关）【不复用】：用户先寒暄"你好"再报障"vpn连不上"，
      复用了 other 会把真正的报障锁死在"非支持场景"→ 错误转 rag_query。
      寒暄是"还没开始说正事"，每轮都必须重新识别。
    """
    if state.get("intent") and state.get("intent") != "other":
        # 复用分支同时清除 multi_scenarios：多问题只在首轮检测，
        # 不清理会残留导致后续轮次每轮都走多问题引导（死循环）
        return {
            "multi_scenarios": None,
            "trace": [{
                "node": "intent",
                "result": {"reused": state["intent"],
                           "request_type": state.get("request_type", "troubleshoot")},
            }],
        }

    user_msg = state["messages"][-1]["content"]

    reply = chat_json([
        {"role": "system", "content": INTENT_PROMPT},
        {"role": "user", "content": user_msg},
    ], model_chain=state.get("model_chain"))

    intent = reply.get("intent", "other")
    if intent not in [*list(SCENARIOS), "other"]:
        intent = "other"
    request_type = reply.get("request_type", "troubleshoot")
    if request_type not in ("troubleshoot", "consult", "other"):
        request_type = "troubleshoot"  # 非法值归一化：默认故障（宁多问不漏报障）

    # 拼写纠错兜底：LLM 判 other 时，规则层找近似关键词（bpn→vpn）。
    # 打字错误 ≠ 业务不受支持，直接转人工不合理——修正后走正常流程。
    # 兜底在【规则层】而非 LLM：编辑距离确定性可测试，免费模型不稳定。
    if intent == "other":
        fix = _fuzzy_fix_intent(user_msg, intent)
        if fix:
            intent = fix["intent"]
            # 场景词通常是报障（"bpn"=想修 VPN）；寒暄判定不再成立
            if request_type == "other":
                request_type = "troubleshoot"
            reply = {**reply, "correction": fix["correction"]}

    # 多问题检测：一条消息含 ≥2 个场景关键词（如 "vpn和密码都连不上"）。
    # intent 是单一值装不下多个诉求——交给 multi 节点引导逐个描述
    # （真实客服同样做法；之前这种消息会转人工，不合理）。
    multi = _detect_multi(user_msg)
    if multi:
        reply = {**reply, "multi_scenarios": multi}

    return {
        "intent": intent,
        "request_type": request_type,
        "multi_scenarios": multi,
        "trace": [{
            "node": "intent",
            "result": reply,
        }],
    }


def _detect_multi(user_msg: str) -> list[str] | None:
    """多问题检测：消息里精确命中 ≥2 个不同场景的关键词 → 返回场景列表。

    规则层（与拼写纠错同理）：确定性可测试，不依赖免费模型的分类能力。
    """
    msg = user_msg.lower()
    found: list[str] = []
    for key, sc in SCENARIOS.items():
        if any(kw.lower() in msg for kw in sc["keywords"]):
            found.append(key)
    return found if len(found) >= 2 else None

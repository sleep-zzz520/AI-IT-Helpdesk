"""⑤风险分级节点：决定方案能不能自动执行（信任边界）。

分级策略（配置）：风险等级 → 动作。
加等级 = 改一行，不用动逻辑。
"""
from app.agents.state import HelpdeskState

# 信任边界：什么风险等级可以自动执行
RISK_POLICY = {
    "low": "auto",     # 低风险 → 自动执行
    "medium": "human", # 中风险 → 转人工审批
    "high": "human",   # 高风险 → 转人工审批
}

# 兜底：未知风险默认最高级（安全优先：不确定就当高风险）
DEFAULT_RISK = "high"


def risk_node(state: HelpdeskState) -> dict:
    kb = state.get("kb_match", {})
    if not kb.get("matched"):
        # 知识库未命中 → 没有可执行的方案 → 必须转人工
        decision = "human"
        reason = f"知识库未命中：{kb.get('reason')}"
    else:
        risk = kb.get("risk", DEFAULT_RISK)
        decision = RISK_POLICY.get(risk, "human")
        reason = f"方案「{kb.get('solution')}」风险等级={risk}"

    return {
        "risk_level": decision,  # auto / human
        "trace": [{
            "node": "risk",
            "result": {"decision": decision, "reason": reason},
        }],
    }

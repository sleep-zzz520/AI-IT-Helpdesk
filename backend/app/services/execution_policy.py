"""执行风险策略：把“能不能自动执行”从某个入口中抽离出来。

LangGraph 的 ``risk_node`` 和未来的 MCP 写操作都应该调用这里的纯规则，
而不是各自维护一份 low / medium / high 的判断。
"""

AUTO_EXECUTION = "auto"
HUMAN_REVIEW = "human"

# 信任边界：什么风险等级可以自动执行。
RISK_POLICY = {
    "low": AUTO_EXECUTION,
    "medium": HUMAN_REVIEW,
    "high": HUMAN_REVIEW,
}

# 不认识的风险等级不执行。这条兜底必须与入口无关。
DEFAULT_RISK = "high"


def decide_execution(kb_match: dict) -> tuple[str, str]:
    """根据已命中的知识库方案给出自动执行或人工审批决定。

    这是纯函数：不访问数据库、不调用模型，也不执行工具，因此网页 Agent
    和未来的 MCP Handler 可以得到完全相同的安全决定。
    """
    if not kb_match.get("matched"):
        return HUMAN_REVIEW, f"知识库未命中：{kb_match.get('reason')}"

    risk = kb_match.get("risk", DEFAULT_RISK)
    decision = RISK_POLICY.get(risk, HUMAN_REVIEW)
    return decision, f"方案「{kb_match.get('solution')}」风险等级={risk}"

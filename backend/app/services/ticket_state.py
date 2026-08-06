"""工单状态机：定义合法状态 + 转移规则 + 护栏（防非法流转）。

【为什么用状态机而不是 if/else？】
1. 显式：所有合法转移集中在一张表，一眼看懂工单能怎么走
2. 防错：非法转移（如 resolved→handoff 直接跳、new→resolved 跳过处理中）
   被 can_transition 挡住——if/else 散落各处，漏一个校验就是 bug
3. 可扩展：加状态（如 cancelled/closed）= 加一行状态 + 几行转移
4. 面试点：状态机是"确定性流程"的工程化表达，和 LangGraph 的节点+条件边同源

【状态转移图】
    new ──收到用户消息──▶ processing ──执行成功──▶ resolved
     │                        │                    │
     │                        └──风险/失败/异常──▶ handoff
     │                                           （终态：转人工）
     └──────────风险/失败/异常──▶ handoff
    resolved ──用户反馈"未解决"──▶ processing（重新打开，闭环）

【护栏策略】
- can_transition 是纯函数（无副作用），任何推进前先问它
- 非法转移返回 False，调用方记日志并保持原状态（不静默改，也不抛异常
  打断业务——状态机是护栏不是路障）
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ===== 状态定义 =====
STATUS_NEW = "new"                # 新建（会话刚创建，还没开始处理）
STATUS_PROCESSING = "processing"  # 处理中（用户已发消息，Agent 正在跑）
STATUS_RESOLVED = "resolved"      # 已解决（执行成功，收尾关单）
STATUS_HANDOFF = "handoff"        # 转人工（终态：人工工单接管）

# 全部合法状态（校验用 + 前端枚举）
VALID_STATUS = {STATUS_NEW, STATUS_PROCESSING, STATUS_RESOLVED, STATUS_HANDOFF}

# ===== 转移表：from → 允许的 to 集合 =====
# 每一行都是一个"业务规则"：
# - 终态（handoff）没有出口 → 空集合（人工工单不回流 Agent）
# - resolved 允许回到 processing：用户反馈"未解决"要重新处理
TRANSITIONS: dict[str, set[str]] = {
    STATUS_NEW: {STATUS_PROCESSING, STATUS_HANDOFF},
    STATUS_PROCESSING: {STATUS_RESOLVED, STATUS_HANDOFF},
    STATUS_RESOLVED: {STATUS_PROCESSING},
    STATUS_HANDOFF: set(),  # 终态
}

# 展示用中文名（前端也可复用这个语义）
STATUS_LABELS = {
    STATUS_NEW: "新建",
    STATUS_PROCESSING: "处理中",
    STATUS_RESOLVED: "已解决",
    STATUS_HANDOFF: "转人工",
}


def is_valid(status: str) -> bool:
    """是不是合法的状态值（防脏数据/拼写错误写进库）。"""
    return status in VALID_STATUS


def can_transition(from_status: str, to_status: str) -> bool:
    """护栏：from_status 能否合法转移到 to_status。

    纯函数、无副作用——任何推进状态的地方先问它。
    参数不合法（未知状态）也返回 False（防御：宁可拒绝不可放行）。
    """
    if not is_valid(from_status) or not is_valid(to_status):
        return False
    return to_status in TRANSITIONS.get(from_status, set())


def assert_transition(from_status: str, to_status: str) -> None:
    """开发期校验用：非法转移直接抛错（写测试/排查时用）。

    生产路径不用它（用 can_transition + 记日志），它给开发兜底：
    状态机改动时，测试里能立刻暴露"漏了转移规则"。
    """
    if not can_transition(from_status, to_status):
        raise ValueError(
            f"非法工单状态转移: {from_status} -> {to_status} "
            f"（合法目标: {sorted(TRANSITIONS.get(from_status, set()))}）"
        )

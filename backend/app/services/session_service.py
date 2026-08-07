"""会话服务：数据库和 Agent 之间的翻译官。

FastAPI 接口以后只需调这三个函数，不用碰 SQL：
- create_conversation: 开新会话
- save_turn: 存这一轮新增的消息 + Trace + 工单状态机推进
- load_state: 从库里重建 state（恢复会话记忆）
"""
import logging

from sqlalchemy.orm import Session

from app.models import Conversation, Message, TicketStatusLog, Trace
from app.services import ticket_state

logger = logging.getLogger(__name__)


def create_conversation(db: Session, user_id: str,
                        tenant_id: int | None = None) -> Conversation:
    """开新会话。tenant_id 由调用方（登录用户身份）注入。

    多租户：user_id 是"谁"，tenant_id 是"哪个租户"——两者都来自登录态，
    不信任前端传值（防伪造他人身份/跨租户数据）。
    """
    conv = Conversation(user_id=user_id, tenant_id=tenant_id)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def save_turn(db: Session, conv: Conversation, state: dict, elapsed_ms: int | None = None) -> None:
    """把这一轮【新增】的消息和 Trace 追加进库。

    用"长度对比"只存新增部分：messages/trace 都是 append-only，
    已存 N 条，state 里第 N 条之后的才是本轮新增。
    elapsed_ms：本轮 Agent 总耗时，挂在本轮新增的 assistant 消息上。

    约束：**同一会话需串行调用**（前端 sending 状态已挡单页连点）。
    并发多请求下长度对比基于各自 Session 快照，可能重复追加（罕见场景，
    演示项目不引入行锁；生产可换 SELECT ... FOR UPDATE 串行化）。
    """
    new_msgs = state.get("messages", [])[len(conv.messages):]
    for m in new_msgs:
        # 图片消息不落库 base64（占空间）：内容标记 [图片]，OCR 结果已进 error_code
        content = "[图片] " + m["content"] if m.get("image") else m["content"]
        conv.messages.append(Message(
            role=m["role"], content=content,
            elapsed_ms=elapsed_ms if m["role"] == "assistant" else None,
        ))

    new_traces = state.get("trace", [])[len(conv.traces):]
    for t in new_traces:
        conv.traces.append(Trace(
            node=t["node"], result=t["result"], elapsed_ms=t.get("elapsed_ms"),
        ))

    # 会话级字段：intent 复用、业务字段落库、工单状态机推进
    conv.intent = state.get("intent", conv.intent)
    conv.request_type = state.get("request_type", conv.request_type)
    for field in ("device", "error_code", "username"):
        if state.get(field):
            setattr(conv, field, state[field])
    conv.ticket_id = state.get("ticket_id", conv.ticket_id)
    _advance_status(db, conv, state)
    db.commit()


def _advance_status(db: Session, conv: Conversation, state: dict) -> None:
    """工单状态机推进（核心新增）：按业务事件决定目标状态 + 落流转日志。

    三步走（对应状态机的"显式、防错、可追溯"）：
    1. 从 state 推导【目标状态】（业务规则，优先级从上到下）
    2. 问状态机护栏：当前状态能不能到目标状态（can_transition）
    3. 能 → 记录 from→to + 原因到 ticket_status_logs（履历），更新当前状态

    【为什么不直接 if/else 改 conv.status？】
    所有推进都过状态机，非法流转（如 new→resolved 跳过处理中）
    被挡下并记 warning——改状态机的"状态"和"转移"是一处，不会漏。

    【优先级说明】
    - handoff（转人工）优先级最高：风险审批/执行失败/系统异常都是终态信号
    - resolved（已解决）：只有拿到 ticket_id（close 节点成功）才算
    - processing（处理中）：本轮有新的用户消息，工单进入处理
    - 用户"未解决"反馈：resolved → processing（重新打开，状态机闭环）
    """
    target = None
    reason = None

    # ① 转人工（终态信号，最优先）
    if state.get("risk_level") == "human":
        target, reason = ticket_state.STATUS_HANDOFF, "风险需人工审批或知识库未命中"
    # ② 已解决（执行成功，close 节点生成了 ticket_id）
    elif state.get("ticket_id") and state.get("tool_result", {}).get("status") == "ok":
        target, reason = ticket_state.STATUS_RESOLVED, "执行成功，收尾关单"
    # ③ 处理中（本轮有新用户消息；新建会话首轮消息触发 new→processing）
    elif state.get("new_user_message") and conv.status == ticket_state.STATUS_NEW:
        target, reason = ticket_state.STATUS_PROCESSING, "收到用户问题，开始处理"
    # ④ 重新打开（用户明确反馈"未解决"，resolved→processing 闭环）
    elif _is_reopen_request(state):
        target, reason = ticket_state.STATUS_PROCESSING, "用户反馈未解决，重新打开"

    if target is None or target == conv.status:
        return  # 没有状态变化

    # 状态机护栏：非法转移 → 记 warning 保持原状态（不静默改，不打断业务）
    if not ticket_state.can_transition(conv.status, target):
        logger.warning("状态机拦截非法转移: conv=%s %s->%s",
                       conv.id, conv.status, target)
        return

    # 合法推进：落流转历史（履历） + 更新当前状态
    db.add(TicketStatusLog(
        conversation_id=conv.id,
        from_status=conv.status,
        to_status=target,
        reason=reason,
    ))
    conv.status = target


def _is_reopen_request(state: dict) -> bool:
    """检测用户是否反馈"未解决"（resolved→processing 闭环的触发条件）。

    只在已解决状态且本轮有用户消息时检查（否则新会话也误判）。
    规则匹配放最前两个词，避免长回复误伤（"这个问题虽然未解决但我先..."）。
    """
    if state.get("new_user_message") is None:
        return False
    content = state["new_user_message"].strip()
    return content.startswith(("未解决", "没有解决", "还没好", "没解决"))


def load_state(db: Session, conv_id: int) -> dict:
    """从库里重建 state（恢复会话记忆）。"""
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise ValueError(f"会话不存在: {conv_id}")

    state = {
        "messages": [
            {"role": m.role, "content": m.content, "elapsed_ms": m.elapsed_ms}
            for m in conv.messages
        ],
        "trace": [
            {"node": t.node, "result": t.result, "elapsed_ms": t.elapsed_ms}
            for t in conv.traces
        ],
    }
    # 会话级字段必须完整重建，漏一个 = Agent 失忆
    if conv.user_id:
        state["user_id"] = conv.user_id
    if conv.intent:
        state["intent"] = conv.intent
    if conv.request_type:
        state["request_type"] = conv.request_type
    for field in ("device", "error_code", "username"):
        if getattr(conv, field, None):
            state[field] = getattr(conv, field)
    if conv.ticket_id:
        state["ticket_id"] = conv.ticket_id
    return state

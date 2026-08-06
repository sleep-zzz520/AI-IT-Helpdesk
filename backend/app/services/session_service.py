"""会话服务：数据库和 Agent 之间的翻译官。

FastAPI 接口以后只需调这三个函数，不用碰 SQL：
- create_conversation: 开新会话
- save_turn: 存这一轮新增的消息 + Trace
- load_state: 从库里重建 state（恢复会话记忆）
"""
from sqlalchemy.orm import Session

from app.models import Conversation, Message, Trace


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

    # 会话级字段：intent 复用、业务字段落库、工单状态推进
    conv.intent = state.get("intent", conv.intent)
    for field in ("device", "error_code", "username"):
        if state.get(field):
            setattr(conv, field, state[field])
    conv.ticket_id = state.get("ticket_id", conv.ticket_id)
    if state.get("risk_level") == "human":
        conv.status = "handoff"
    elif state.get("ticket_id"):
        conv.status = "resolved"
    db.commit()


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
    for field in ("device", "error_code", "username"):
        if getattr(conv, field, None):
            state[field] = getattr(conv, field)
    if conv.ticket_id:
        state["ticket_id"] = conv.ticket_id
    return state

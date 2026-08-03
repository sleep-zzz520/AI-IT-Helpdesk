"""会话服务：数据库和 Agent 之间的翻译官。

FastAPI 接口以后只需调这三个函数，不用碰 SQL：
- create_conversation: 开新会话
- save_turn: 存这一轮新增的消息 + Trace
- load_state: 从库里重建 state（恢复会话记忆）
"""
from sqlalchemy.orm import Session

from app.models import Conversation, Message, Trace


def create_conversation(db: Session, user_id: str) -> Conversation:
    conv = Conversation(user_id=user_id)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def save_turn(db: Session, conv: Conversation, state: dict) -> None:
    """把这一轮【新增】的消息和 Trace 追加进库。

    用"长度对比"只存新增部分：messages/trace 都是 append-only，
    已存 N 条，state 里第 N 条之后的才是本轮新增。
    """
    new_msgs = state.get("messages", [])[len(conv.messages):]
    for m in new_msgs:
        conv.messages.append(Message(role=m["role"], content=m["content"]))

    new_traces = state.get("trace", [])[len(conv.traces):]
    for t in new_traces:
        conv.traces.append(Trace(node=t["node"], result=t["result"]))

    # 会话级字段：intent 复用、工单状态推进
    conv.intent = state.get("intent", conv.intent)
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
        "messages": [{"role": m.role, "content": m.content} for m in conv.messages],
        "trace": [{"node": t.node, "result": t.result} for t in conv.traces],
    }
    # 会话级字段必须完整重建，漏一个 = Agent 失忆
    if conv.user_id:
        state["user_id"] = conv.user_id
    if conv.intent:
        state["intent"] = conv.intent
    if conv.ticket_id:
        state["ticket_id"] = conv.ticket_id
    return state

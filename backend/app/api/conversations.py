"""会话相关 API 路由：创建会话 / 发消息 / 查 Trace。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agents.graph import graph
from app.db import SessionLocal
from app.models import Conversation
from app.schemas import (
    ConversationCreate,
    ConversationOut,
    MessageCreate,
    MessageReply,
    TraceOut,
)
from app.services import session_service

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def get_db():
    """依赖注入：每个请求开一个数据库会话，用完自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("", response_model=ConversationOut)
def create_conversation(body: ConversationCreate, db: Session = Depends(get_db)):
    """开新工单/会话。"""
    conv = session_service.create_conversation(db, body.user_id)
    return conv


@router.post("/{conv_id}/messages", response_model=MessageReply)
def send_message(conv_id: int, body: MessageCreate, db: Session = Depends(get_db)):
    """发消息：恢复记忆 → 跑 Agent → 存库 → 返回回复。"""
    # 1. 从数据库恢复会话记忆
    state = session_service.load_state(db, conv_id)
    # 2. 把新消息追加进历史
    state["messages"] = state.get("messages", []) + [{"role": "user", "content": body.content}]
    # 3. 跑一轮 Agent（完整状态图）
    out = graph.invoke(state)
    # 4. 存回数据库（新增消息 + Trace）
    conv = db.get(Conversation, conv_id)
    session_service.save_turn(db, conv, out)
    # 5. 取最新 assistant 回复
    replies = [m for m in out["messages"] if m["role"] == "assistant"]
    reply = replies[-1]["content"] if replies else "（无回复）"
    return MessageReply(conversation_id=conv_id, reply=reply, messages=out["messages"])


@router.get("/{conv_id}/traces", response_model=list[TraceOut])
def get_traces(conv_id: int, db: Session = Depends(get_db)):
    """查会话的 Trace 记录（可视化面板数据源）。"""
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv.traces

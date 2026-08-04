"""ORM 数据模型：三张表（会话 / 消息 / Trace）。

设计要点：
- conversations 存会话级字段（intent 是会话级判断，必须落库）
- messages 全量存对话历史（extract 每轮从历史重抽业务字段，无需单独存）
- traces 存每节点执行记录（Trace 可视化面板的数据源）
- 一对多关系：conversation → messages / traces，靠外键关联
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    # 会话级状态：intent 判定一次后复用（见优化文档「意图是会话级状态」）
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 业务字段必须持久化：图片等不可重抽来源的信息，落库才能跨轮记忆
    device: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 工单状态：open（进行中）/ resolved（已解决）/ handoff（转人工）
    status: Mapped[str] = mapped_column(String(16), default="open")
    ticket_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan")
    traces: Mapped[list["Trace"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    role: Mapped[str] = mapped_column(String(16))      # user / assistant / system
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Trace(Base):
    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    node: Mapped[str] = mapped_column(String(32))      # 节点名：intent/extract/...
    result: Mapped[dict] = mapped_column(JSON)          # 节点结果（可含 token 消耗等）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="traces")

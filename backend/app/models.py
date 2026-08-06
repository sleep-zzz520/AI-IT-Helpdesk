"""ORM 数据模型：三张表（会话 / 消息 / Trace）。

设计要点：
- conversations 存会话级字段（intent 是会话级判断，必须落库）
- messages 全量存对话历史（extract 每轮从历史重抽业务字段，无需单独存）
- traces 存每节点执行记录（Trace 可视化面板的数据源）
- 一对多关系：conversation → messages / traces，靠外键关联
"""
from datetime import datetime

from sqlalchemy import (
    JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, func,
)
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
    # 本轮 Agent 回复的总耗时（ms，assistant 消息才有值；可观测性用）
    elapsed_ms: Mapped[int | None] = mapped_column(nullable=True)
    # 用户反馈（assistant 消息才有值）：up（有用）/ down（没用）。null = 未反馈
    # 反馈闭环数据源：负反馈分析从这里读，再结合 trace 判断文档缺失 vs 过时
    feedback: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Trace(Base):
    __tablename__ = "traces"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    node: Mapped[str] = mapped_column(String(32))      # 节点名：intent/extract/...
    result: Mapped[dict] = mapped_column(JSON)          # 节点结果（可含 token 消耗等）
    # 该节点的执行耗时（ms）——执行链路时间线数据源
    elapsed_ms: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="traces")


class KbDocument(Base):
    """RAG 知识库台账：源文档与向量库的同步状态（知识库生命周期管理）。

    设计要点：
    - path（相对路径）是文档的唯一身份；doc_id 是路径哈希（稳定不随内容变化）
    - doc_hash（内容 sha256）是变更检测的依据：hash 变了 → 该文档需重建
    - status: active（在库）/ removed（已删除，保留审计痕迹）
    - changelog 记录最近一次 sync 对该文档做了什么（审计可追溯）
    """

    __tablename__ = "kb_documents"
    # 复合唯一：(kb_name, path) —— 多知识库实例的同名路径互不冲突
    __table_args__ = (
        UniqueConstraint("kb_name", "path", name="uq_kb_name_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 知识库实例名（默认 default；测试/多知识库用独立名隔离台账，互不误判删除）
    kb_name: Mapped[str] = mapped_column(String(32), default="default")
    path: Mapped[str] = mapped_column(String(255))
    doc_id: Mapped[str] = mapped_column(String(16))  # 同 path 不同 kb 会重复，不设全局唯一
    doc_hash: Mapped[str] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")
    # 文档有效期（YYYYMMDD，来自 frontmatter valid_to）：过期预警数据源
    # 为什么冗余存台账：读源文件要解析/转译（媒体文档调 GLM-4V，重），
    # 台账快照让「列表/预警」只查 MySQL，永远不碰源文件与向量库
    valid_to: Mapped[str | None] = mapped_column(String(16), nullable=True)
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())

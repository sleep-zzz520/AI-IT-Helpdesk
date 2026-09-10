"""ORM 数据模型：身份、会话、审计、工单履历和执行台账。

设计要点：
- conversations 存会话级字段（intent 是会话级判断，必须落库）
- messages 全量存对话历史（extract 每轮从历史重抽业务字段，无需单独存）
- traces 存每节点执行记录（Trace 可视化面板的数据源）
- 一对多关系：conversation → messages / traces，靠外键关联
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Tenant(Base):
    """租户：多租户隔离的顶层单位（一个租户 = 一个企业/部门）。

    - code 是稳定业务标识（API/代码里用），name 是展示名
    - 所有业务数据（用户/会话/工单）通过 tenant_id 归属租户
    """
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # 业务码：acme / globex
    name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    """平台用户：登录身份 + 角色 + 租户归属。

    - password_hash：PBKDF2 哈希（绝不明文存密码，见 security.py）
    - role: admin（管理员，可审计/管理）/ user（普通用户，只能操作本租户数据）
    - tenant_id：数据隔离的关键——用户只能访问自己租户的数据
    - active=False 可禁用账号（踢出登录）
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin / user
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    active: Mapped[int] = mapped_column(default=1)  # 1=启用 0=禁用
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditLog(Base):
    """审计日志：谁在什么时候做了什么（安全可追溯）。

    - action：动作名（login / create_conversation / send_message / execute_tool / kb_sync ...）
    - detail：JSON 细节（如执行了什么工具、同步结果摘要）
    - ip：来源 IP（网络安全基本盘）
    - 只追加、不修改、不删除（审计日志的不可变原则）
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    user_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ExecutionOperation(Base):
    """有副作用操作的幂等台账。

    ``operation_id`` 全局唯一：同一个调用重试只能读取第一次的结果，不能再次
    调用外部系统。它与 AuditLog 的关系是：台账回答“能否重放/当前状态”，审计
    回答“谁做过什么”。
    """
    __tablename__ = "execution_operations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    action: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(64))
    target_user_id: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # pending / succeeded / failed
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now())


class TicketStatusLog(Base):
    """工单状态流转历史：每一次 from→to 都留痕（状态机可追溯）。

    与 AuditLog 的区别：AuditLog 记"人做了什么操作"（登录/发消息），
    这个表记"工单状态机本身怎么走的"（new→processing→resolved）——
    是工单的"履历"，查询状态机轨迹用这张表。
    """
    __tablename__ = "ticket_status_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    conversation: Mapped["Conversation"] = relationship(back_populates="status_logs")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64))
    # 多租户隔离：会话归属租户（由登录用户身份注入，不是前端传的）
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    # 会话级状态：intent 判定一次后复用（见优化文档「意图是会话级状态」）
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 诉求类型（troubleshoot/consult/other）：路由的关键依据。
    # 踩坑：之前只存 intent 不存 request_type，跨轮 load_state 时 request_type
    # 丢失 → 寒暄后报障被错误路由到 rag_query（见优化文档，2026-08-07）
    request_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 业务字段必须持久化：图片等不可重抽来源的信息，落库才能跨轮记忆
    device: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 工单状态（状态机，见 services/ticket_state.py）：
    # new（新建）→ processing（处理中）→ resolved（已解决）/ handoff（转人工）
    # resolved --"未解决"--> processing（重新打开）
    status: Mapped[str] = mapped_column(String(16), default="new")
    ticket_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan")
    traces: Mapped[list["Trace"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan")
    status_logs: Mapped[list["TicketStatusLog"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan",
        order_by="TicketStatusLog.id")


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

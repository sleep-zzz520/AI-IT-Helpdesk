"""Pydantic 数据模型：API 请求/响应的「契约」。

作用：自动校验（类型错了直接 422）+ 自动生成 /docs 文档。
"""
from typing import Literal

from pydantic import BaseModel


# ===== 请求 =====
class ConversationCreate(BaseModel):
    user_id: str


class MessageCreate(BaseModel):
    content: str
    image: str | None = None  # 可选：报错截图的完整 data URL（自带 MIME，OCR 用）
    # 模型链模式：fast（速度优先，glm-4-flash 打头）/ accurate（能力优先，默认）
    mode: Literal["fast", "accurate"] = "accurate"


# ===== 响应 =====
class MessageOut(BaseModel):
    role: str
    content: str
    elapsed_ms: int | None = None  # agent 回复总耗时（ms，仅 assistant 消息有）


class TraceOut(BaseModel):
    node: str
    result: dict
    elapsed_ms: int | None = None  # 节点执行耗时（ms）


class ConversationOut(BaseModel):
    id: int
    user_id: str
    intent: str | None = None
    status: str
    ticket_id: str | None = None


class MessageReply(BaseModel):
    conversation_id: int
    reply: str                      # 最新 assistant 回复
    messages: list[MessageOut]      # 完整对话历史（前端渲染用）

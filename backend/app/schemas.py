"""Pydantic 数据模型：API 请求/响应的「契约」。

作用：自动校验（类型错了直接 422）+ 自动生成 /docs 文档。
"""
from pydantic import BaseModel


# ===== 请求 =====
class ConversationCreate(BaseModel):
    user_id: str


class MessageCreate(BaseModel):
    content: str
    image: str | None = None  # 可选：报错截图的 base64（OCR 识别错误码用）


# ===== 响应 =====
class MessageOut(BaseModel):
    role: str
    content: str


class TraceOut(BaseModel):
    node: str
    result: dict


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

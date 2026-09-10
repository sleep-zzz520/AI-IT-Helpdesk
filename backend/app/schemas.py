"""Pydantic 数据模型：API 请求/响应的「契约」。

作用：自动校验（类型错了直接 422）+ 自动生成 /docs 文档。
"""
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.services.llm_config_service import normalize_base_url


# ===== 请求 =====
class LoginRequest(BaseModel):
    username: str
    password: str


class ConversationCreate(BaseModel):
    # user_id 可选：不传则用登录用户（推荐）；传了仅管理员可替他人开（暂不开放）
    user_id: str | None = None


class MessageCreate(BaseModel):
    content: str
    image: str | None = None  # 可选：报错截图的完整 data URL（自带 MIME，OCR 用）
    # 模型链模式：fast（速度优先，glm-4-flash 打头）/ accurate（能力优先，默认）
    mode: Literal["fast", "accurate"] = "accurate"
    # 空值 = 项目默认 GLM 模型链；非空值必须属于当前登录用户。
    llm_config_id: int | None = Field(default=None, ge=1)


class LLMConfigBase(BaseModel):
    """用户配置模型时可见的非敏感字段。"""
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=128)
    json_mode: bool = True

    @field_validator("name", "model")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_base_url(value)


class LLMConfigCreate(LLMConfigBase):
    # 只允许在创建/测试请求中出现，任何响应模型都不含它。
    api_key: str = Field(min_length=1, max_length=4096)

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class LLMConfigOut(LLMConfigBase):
    id: int
    has_api_key: bool
    created_at: str | None = None
    updated_at: str | None = None


class LLMConnectionTestOut(BaseModel):
    ok: bool
    latency_ms: int


class FeedbackCreate(BaseModel):
    # 👍 有用 / 👎 没用；null = 取消反馈（点错可改：再点已选值 → 取消）
    feedback: Literal["up", "down"] | None


# ===== 响应 =====
class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str                    # admin / user
    tenant_id: int


class TokenResponse(BaseModel):
    token: str
    user: UserOut


class MessageOut(BaseModel):
    id: int | None = None       # 消息 id（👍/👎 反馈需要；历史消息必有）
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


class StatusLogOut(BaseModel):
    id: int
    from_status: str
    to_status: str
    reason: str | None = None
    created_at: str | None = None


class MessageReply(BaseModel):
    conversation_id: int
    reply: str                      # 最新 assistant 回复
    messages: list[MessageOut]      # 完整对话历史（前端渲染用）

"""用户自定义模型配置的安全边界。

模型服务沿用 OpenAI 兼容协议，但 API Key 是用户私有凭据：只在后端接收、
Fernet 加密后入库，并且不进入 API 响应、审计日志、会话消息或 Trace。
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from ipaddress import ip_address
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.models import LLMConfig


class LLMConfigSecretError(RuntimeError):
    """密钥加密环境未就绪或密文已无法解密。"""


@dataclass(frozen=True)
class LLMProfile:
    """一次请求所需的运行时模型资料，不会写入会话或 Trace。"""
    config_id: int | None
    name: str
    base_url: str
    model: str
    api_key: str
    json_mode: bool


def normalize_base_url(value: str) -> str:
    """校验并规范化 OpenAI 兼容服务地址。

    默认只允许 HTTPS 公网 URL，防止普通用户把后端当作访问内网的跳板。
    本地 Ollama/vLLM 仅在部署者显式设置 ``LLM_ALLOW_PRIVATE_ENDPOINTS=true``
    时可用；该开关应只在单机或受控内网部署启用。
    """
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("请输入完整的 http(s) OpenAI 兼容地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("模型地址不能包含账号、查询参数或片段")

    host = parsed.hostname.lower()
    is_private = host == "localhost" or host.endswith(".localhost")
    # 域名会在连接时由 SDK 解析；公网 HTTPS 域名是常见 SaaS 供应商形式。
    with suppress(ValueError):
        is_private = is_private or not ip_address(host).is_global

    if not settings.LLM_ALLOW_PRIVATE_ENDPOINTS and (parsed.scheme != "https" or is_private):
        raise ValueError("默认仅允许 HTTPS 公网模型地址；本地模型需由部署者开启 LLM_ALLOW_PRIVATE_ENDPOINTS")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _fernet() -> Fernet:
    key = settings.LLM_CONFIG_ENCRYPTION_KEY.strip()
    if not key:
        raise LLMConfigSecretError(
            "服务器尚未配置 LLM_CONFIG_ENCRYPTION_KEY，不能安全保存用户 API Key"
        )
    try:
        return Fernet(key.encode())
    except (TypeError, ValueError) as exc:
        raise LLMConfigSecretError("LLM_CONFIG_ENCRYPTION_KEY 不是有效的 Fernet 密钥") from exc


def encrypt_api_key(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode()).decode()


def profile_from_config(config: LLMConfig) -> LLMProfile:
    """临时解密当前请求要用的密钥，绝不把密钥放进响应或持久化状态。"""
    try:
        api_key = _fernet().decrypt(config.api_key_ciphertext.encode()).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise LLMConfigSecretError("该模型配置的密钥无法解密，请删除后重新添加") from exc
    return LLMProfile(
        config_id=config.id,
        name=config.name,
        base_url=config.base_url,
        model=config.model,
        api_key=api_key,
        json_mode=config.json_mode,
    )


def profile_from_values(
    *, name: str, base_url: str, model: str, api_key: str, json_mode: bool,
) -> LLMProfile:
    """连接测试使用的短生命周期资料，不落库。"""
    return LLMProfile(
        config_id=None,
        name=name,
        base_url=base_url,
        model=model,
        api_key=api_key,
        json_mode=json_mode,
    )


def test_connection(profile: LLMProfile) -> int:
    """向用户选择的模型发送极短请求，返回耗时毫秒。

    这是真实供应商请求，前端必须提示可能按供应商规则计费。
    """
    from app.llm import chat_with_profile

    started = perf_counter()
    chat_with_profile(
        profile,
        messages=[{"role": "user", "content": "ping"}],
        temperature=0,
        max_tokens=1,
    )
    return round((perf_counter() - started) * 1000)

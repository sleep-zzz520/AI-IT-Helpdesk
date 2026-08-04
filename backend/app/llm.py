"""LLM 客户端：用 openai SDK 指向智谱 GLM 的 OpenAI 兼容端点。

设计要点：
- 模型无关：换 DeepSeek / OpenAI / 本地模型，只改 .env，代码零改动
- 故障转移（failover）：模型链按优先级排列，限流/连接失败/模型不存在
  时【立即切换下一个模型】，不再 sleep 退避白等——免费模型限流的现实解法
"""
import json
import re

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)

from app.config import settings

client = OpenAI(
    api_key=settings.ZHIPU_API_KEY,
    base_url=settings.GLM_BASE_URL,
)

# 进程级调用计数器（Eval 报告/审计用）
CALL_COUNT = 0
# 最近一次实际使用的模型（可观测性）
LAST_MODEL = None

# 触发切换的异常：网络/超时/所有 HTTP 错误（429 限流、403 无权限、
# 404 模型不存在、5xx 服务端错误都算"模型不可用"→ 切下一个）
_FAILOVER_EXCEPTIONS = (APIConnectionError, APITimeoutError, APIStatusError)


def _create(model_chain: list[str], **kwargs):
    """按模型链调用，失败立即切换下一个。全部失败抛最后一个错误。"""
    global CALL_COUNT, LAST_MODEL
    last_error = None
    for model in model_chain:
        try:
            resp = client.chat.completions.create(model=model, **kwargs)
            CALL_COUNT += 1
            LAST_MODEL = model
            return resp
        except _FAILOVER_EXCEPTIONS as e:  # noqa: PERF203
            last_error = e
            print(f"[llm] 模型 {model} 不可用({type(e).__name__})，切换下一个")
            continue
    raise last_error


def chat(messages: list[dict], temperature: float = 0.3) -> str:
    """最简对话封装。后续 LangGraph Agent 会基于它扩展。"""
    resp = _create(
        settings.GLM_MODELS,
        messages=messages,
        temperature=temperature,
    )
    return resp.choices[0].message.content


def chat_json(messages: list[dict], temperature: float = 0.1) -> dict:
    """让模型输出 JSON 并解析（带容错）。

    用 response_format 强制 JSON 模式，再兜底清洗：
    即使模型偶尔返回 ```json {...} ``` 或夹带废话，也能解析出字典。
    """
    resp = _create(
        settings.GLM_MODELS,
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 容错：剥离 markdown 代码块后提取第一个 {...}
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            return json.loads(m.group())
        raise ValueError(f"模型未返回合法 JSON: {raw[:200]}")


def chat_with_image(image_data_url: str, prompt: str) -> str:
    """带图片调用视觉模型（OCR 用）。

    image_data_url: 完整 data URL（如 data:image/png;base64,xxx）。
    注意：MIME 由 data URL 自带（png/jpeg/webp 都支持），不能写死。
    OpenAI 兼容格式：content 是 [文本, 图片] 列表。
    """
    resp = _create(
        settings.GLM_VISION_MODELS,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        }],
    )
    return resp.choices[0].message.content

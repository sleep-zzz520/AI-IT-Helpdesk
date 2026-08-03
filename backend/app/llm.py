"""LLM 客户端：用 openai SDK 指向智谱 GLM 的 OpenAI 兼容端点。

设计要点：
- 模型无关：换 DeepSeek / OpenAI / 本地模型，只改 .env，代码零改动
- 自动重试：LLM 服务不可靠（429 限流/超时）是常态，调用失败要退避重试，
  不能裸奔让上层 500（对应 ROADMAP P0「异常处理」）
"""
import json
import re
import time

from openai import OpenAI, RateLimitError

from app.config import settings

client = OpenAI(
    api_key=settings.ZHIPU_API_KEY,
    base_url=settings.GLM_BASE_URL,
)

# 进程级调用计数器（Eval 报告/审计用）
CALL_COUNT = 0


def _create_with_retry(**kwargs):
    """调用 GLM，429 限流自动重试（指数退避）。最多 3 次。"""
    global CALL_COUNT
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(**kwargs)
            CALL_COUNT += 1
            return resp
        except RateLimitError:  # noqa: PERF203
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))  # 5s → 10s


def chat(messages: list[dict], temperature: float = 0.3) -> str:
    """最简对话封装。后续 LangGraph Agent 会基于它扩展。"""
    resp = _create_with_retry(
        model=settings.GLM_MODEL,
        messages=messages,
        temperature=temperature,
    )
    return resp.choices[0].message.content


def chat_json(messages: list[dict], temperature: float = 0.1) -> dict:
    """让模型输出 JSON 并解析（带容错）。

    用 response_format 强制 JSON 模式，再兜底清洗：
    即使模型偶尔返回 ```json {...} ``` 或夹带废话，也能解析出字典。
    """
    resp = _create_with_retry(
        model=settings.GLM_MODEL,
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

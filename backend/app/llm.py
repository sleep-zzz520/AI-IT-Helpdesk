"""LLM 客户端：用 openai SDK 指向智谱 GLM 的 OpenAI 兼容端点。

设计要点：模型无关。以后换 DeepSeek / OpenAI / 本地模型，
只需改 .env 里的 GLM_BASE_URL 和 GLM_MODEL，代码零改动。
"""
import json
import re

from openai import OpenAI

from app.config import settings

client = OpenAI(
    api_key=settings.ZHIPU_API_KEY,
    base_url=settings.GLM_BASE_URL,
)


def chat(messages: list[dict], temperature: float = 0.3) -> str:
    """最简对话封装。后续 LangGraph Agent 会基于它扩展。"""
    resp = client.chat.completions.create(
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
    resp = client.chat.completions.create(
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

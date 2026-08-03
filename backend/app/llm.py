"""LLM 客户端：用 openai SDK 指向智谱 GLM 的 OpenAI 兼容端点。

设计要点：模型无关。以后换 DeepSeek / OpenAI / 本地模型，
只需改 .env 里的 GLM_BASE_URL 和 GLM_MODEL，代码零改动。
"""
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

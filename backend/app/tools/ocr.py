"""OCR 工具：从报错截图提取错误码（视觉大模型 GLM-4V，免费）。

为什么用视觉模型而不是传统 OCR（tesseract 等）：
- UI 截图的错误码常藏在弹窗/状态栏里，视觉模型是"看图理解"，识别更准
- 与现有模型链路一致，零额外系统依赖
"""
import json
import re

from app.llm import chat_with_image

OCR_PROMPT = """这是一张 VPN 客户端报错截图。请识别其中的错误码（如 Error 800、错误代码 720）。
只输出 JSON：{"error_code": "800"}；如果截图中没有错误码，输出 {"error_code": ""}"""


def extract_error_code(image_data_url: str) -> str:
    """识别截图中的错误码。失败/没有错误码时返回空字符串（绝不抛异常）。"""
    try:
        raw = chat_with_image(image_data_url, OCR_PROMPT)
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            return json.loads(m.group()).get("error_code", "")
    except Exception:  # noqa: BLE001 —— OCR 失败不应拖垮流程
        pass
    return ""

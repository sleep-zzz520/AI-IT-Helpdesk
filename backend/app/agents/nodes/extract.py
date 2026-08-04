"""信息抽取节点：从对话历史里挖出已知字段。

输入：完整 messages（信息可能散落在多轮对话里）+ 可选截图（最后一条消息的 image 字段）。
输出：只更新【抽到了非空】的字段，空的不覆盖已有值。
"""
from app.agents.state import HelpdeskState
from app.llm import chat_json
from app.tools.ocr import extract_error_code

EXTRACT_PROMPT = """你是信息抽取器。从对话中提取用户提到的 IT 信息字段：
- device: 设备型号（如 Windows 11、macOS）
- error_code: 错误码（如 800）
- username: 用户名/账号

只输出 JSON，格式：{"device": "值或空字符串", "error_code": "值或空字符串", "username": "值或空字符串"}
"""


def extract_node(state: HelpdeskState) -> dict:
    # 把完整对话历史给 AI（不是只有最后一条）
    history = state["messages"]
    reply = chat_json([
        {"role": "system", "content": EXTRACT_PROMPT},
        *history,
    ])

    # 只更新抽到非空值的字段——避免用空字符串把已收集的信息覆盖掉
    updates = {k: v.strip() for k, v in reply.items() if isinstance(v, str) and v.strip()}

    # 截图 OCR：最后一条消息带图且文本抽取没拿到错误码时，用视觉模型补
    last = state["messages"][-1]
    if last.get("image") and not updates.get("error_code"):
        code = extract_error_code(last["image"])
        if code:
            updates["error_code"] = code

    updates["trace"] = [{"node": "extract", "result": reply}]
    return updates

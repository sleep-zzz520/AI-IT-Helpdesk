"""追问节点：把缺的信息拼成一句人话，作为 assistant 消息回复用户。

关键：把问题【追加进 messages】——下一轮用户回答时，
完整历史里就带着这个问题，Agent 天然"记得"自己问过啥。
"""
from app.agents.state import HelpdeskState

# 每个字段对应的提问话术（查表，不用 AI）
ASK_TEMPLATE = {
    "device": "请问您的设备型号是什么？（如 Windows 11 / macOS）",
    "error_code": "请问 VPN 报错时的错误代码是多少？（如 800）",
    "username": "请问您的用户名/账号是什么？",
}


def ask_node(state: HelpdeskState) -> dict:
    question = "；".join(ASK_TEMPLATE[f] for f in state["missing_info"])
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": question}],
        "trace": [{"node": "ask", "result": {"question": question}}],
    }

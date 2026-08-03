"""意图识别节点：流程的「闸门」。

职责：判断用户描述是否属于 VPN 连接故障。
输出：结构化 JSON → 写入 state.intent（vpn / other）。
"""
from app.agents.state import HelpdeskState
from app.llm import chat_json

# 分类标准写死在 prompt 里，让模型按规则判断，而不是瞎猜
INTENT_PROMPT = """你是 IT 运维服务台的意图分类器。
 例如：
【属于 VPN 故障】的典型描述：
- VPN 连不上 / 拨号失败 / 无法建立连接
- Error 800 / 错误代码 800
- 证书过期导致连不上

【不属于】VPN 故障的示例：
- 邮箱/密码问题、电脑死机、软件安装、网络太慢、打印机问题

【属于 密码 故障】的典型描述：
- 密码错误 / 密码过期 / 密码遗忘 / 密码被盗
- 密码重置 / 修改密码 / 忘记密码
- 密码找回 / 修改 / 重置


只输出 JSON，格式：{"intent": "vpn" 或 "password" 或 "other", "reason": "一句话理由"}
"""

#意图分类清单
ClassificationList = {"vpn","password","密码","other"}

def intent_node(state: HelpdeskState) -> dict:
    """输入：最新一条用户消息；输出：intent 分类 + Trace 记录。"""
    user_msg = state["messages"][-1]["content"]

    reply = chat_json([
        {"role": "system", "content": INTENT_PROMPT},
        {"role": "user", "content": user_msg},
    ])

    intent = reply.get("intent","other")
    if intent not in ClassificationList:
        intent = "other"
    

    return {
        "intent": intent,
        "trace": state["trace"]+[{
            "node": "intent", 
            "result": reply,
        }],
    }

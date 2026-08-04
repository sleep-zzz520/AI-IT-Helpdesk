"""⑦收尾节点：执行成功后的最终回复 + 工单结果沉淀。

真实系统里这里会：更新工单状态为「已解决」、通知用户、记录审计日志。
MVP：生成工单号 + 状态写入 Trace，为 P1 持久化铺路。
"""
import time

from app.agents.state import HelpdeskState


def close_node(state: HelpdeskState) -> dict:
    tr = state.get("tool_result", {})
    # 工单号：真实系统由工单系统分配，这里先用时间戳模拟
    ticket_id = f"TKT-{int(time.time())}"

    # 服务台标准收尾话术：结果 + 行动指引 + 兜底
    reply = (
        f"✅ {tr.get('message')}。"
        "请重新连接 VPN 验证。如仍有问题，请回复「未解决」。"
    )
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
        "ticket_id": ticket_id,
        "trace": [{
            "node": "close",
            "result": {"ticket_id": ticket_id, "status": "resolved"},
        }],
    }

"""全链路端到端测试：模拟真实用户「一句话」自动解决 VPN 问题。

这是 AGENTS.md 愿景「45 分钟人工 → 2 分钟自动闭环」的代码验证：
用户描述 → 意图识别 → 信息抽取 → 查证 → 知识库 → 风险分级(auto)
→ 执行续期 → 收尾回复 + 工单沉淀
"""
from app.agents.graph import graph


def run(user_id: str, user_msg: str) -> dict:
    return graph.invoke({
        "messages": [{"role": "user", "content": user_msg}],
        "user_id": user_id,
        "actor_id": user_id,
        "tenant_id": 2 if user_id == "lisi" else 1,
        "execution_source": "web_agent",
        "operation_id": f"script-e2e:{user_id}",
        "trace": [],
    })


if __name__ == "__main__":
    out = run("zhangsan", "VPN连不上，报错Error 800，设备是Windows 11")

    print("=== 全链路执行结果 ===")
    print("意图识别 :", out.get("intent"))
    print("信息抽取 : device =", out.get("device"), "| error_code =", out.get("error_code"))
    print("查证     :", out.get("cert_status"))
    print("知识库   :", out.get("kb_match", {}).get("solution"))
    print("风险分级 :", out.get("risk_level"))
    print("执行 Tool:", out.get("tool_result"))
    print("工单号   :", out.get("ticket_id"))
    print("最终回复 :", out["messages"][-1]["content"])
    print("-" * 60)
    print("Trace 节点:", [t["node"] for t in out.get("trace", [])])

"""⑥执行 Tool 测试：成功 / 身份拒绝。

- zhangsan  + 800 → renew 成功（证书被续期到 2027）
- error_user + 800 → 非登录主体，续期在到达监控系统前被拒绝
"""
from app.agents.graph import graph


def run(user_id: str, error_code: str) -> dict:
    return graph.invoke({
        "messages": [{"role": "user", "content": "VPN连不上"}],
        "intent": "vpn",
        "device": "Windows 11",
        "error_code": error_code,
        "user_id": user_id,
        "actor_id": user_id,
        "tenant_id": 1,
        "execution_source": "web_agent",
        "operation_id": f"script-execute:{user_id}:{error_code}",
        "trace": [],
    })


if __name__ == "__main__":
    cases = [
        ("zhangsan", "800", "执行成功"),
        ("error_user", "800", "身份拒绝 → 转人工"),
    ]
    for uid, code, label in cases:
        out = run(uid, code)
        print(f"[{label}]")
        print("  tool_result:", out.get("tool_result"))
        print("  最终回复  :", out["messages"][-1]["content"])
        print("-" * 60)

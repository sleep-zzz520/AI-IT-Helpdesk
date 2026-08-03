"""⑥执行 Tool 测试：成功 / 失败。

- zhangsan  + 800 → renew 成功（证书被续期到 2027）
- error_user + 800 → renew 失败 → 兜底转人工（执行失败）
"""
from app.agents.graph import graph


def run(user_id: str, error_code: str) -> dict:
    return graph.invoke({
        "messages": [{"role": "user", "content": "VPN连不上"}],
        "intent": "vpn",
        "device": "Windows 11",
        "error_code": error_code,
        "user_id": user_id,
        "trace": [],
    })


if __name__ == "__main__":
    cases = [
        ("zhangsan", "800", "执行成功"),
        ("error_user", "800", "执行失败 → 转人工"),
    ]
    for uid, code, label in cases:
        out = run(uid, code)
        print(f"[{label}]")
        print("  tool_result:", out.get("tool_result"))
        print("  最终回复  :", out["messages"][-1]["content"])
        print("-" * 60)

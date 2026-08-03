"""⑤风险分级测试：三种路径。

- zhangsan + 800 → renew(low)     → auto（自动执行，⑥占位）
- zhangsan + 720 → rebuild(medium) → human（转人工审批）
- zhangsan + 999 → kb 未命中       → human（兜底转人工）
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
        ("zhangsan", "800", "low → 自动执行"),
        ("zhangsan", "720", "medium → 转人工"),
        ("zhangsan", "999", "kb 未命中 → 兜底转人工"),
    ]
    for uid, code, label in cases:
        out = run(uid, code)
        print(f"[{label}]")
        print("  risk_level:", out.get("risk_level"))
        print("  最终回复  :", out["messages"][-1]["content"])
        print("-" * 60)

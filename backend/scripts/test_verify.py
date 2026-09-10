"""③查证测试：覆盖三种结果（过期 / 正常 / 账号不存在）。

注意：这里直接喂齐了 state（intent/device/error_code/user_id），
跳过对话环节，专注测 verify 之后的三分支路由。
"""
from app.agents.graph import graph


def run(user_id: str) -> dict:
    return graph.invoke({
        "messages": [{"role": "user", "content": "VPN连不上，报错800"}],
        "intent": "vpn",
        "device": "Windows 11",
        "error_code": "800",
        "user_id": user_id,
        "actor_id": user_id,
        "tenant_id": 2 if user_id == "lisi" else 1,
        "execution_source": "web_agent",
        "operation_id": f"script-verify:{user_id}",
        "trace": [],
    })


if __name__ == "__main__":
    for uid in ("zhangsan", "lisi", "wangwu"):
        out = run(uid)
        print(f"[{uid}]")
        print("  查证结果 :", out.get("cert_status"))
        print("  最终回复 :", out["messages"][-1]["content"])
        print("-" * 60)

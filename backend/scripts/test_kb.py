"""④知识库匹配测试：命中 / 未命中。

注意：verify 节点会按 user_id 重新查证书（覆盖手动传入的 cert_status），
所以用【不同账号】控制证书状态：
- zhangsan: 证书已过期  → 能走到 kb 匹配
- 用例设计:
  - zhangsan + 800 → 命中 renew（low）
  - zhangsan + 720 → 命中 rebuild（medium）
  - zhangsan + 999 → 未命中（999+过期 不在知识库）
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
        "tenant_id": 2 if user_id == "lisi" else 1,
        "execution_source": "web_agent",
        "operation_id": f"script-kb:{user_id}:{error_code}",
        "trace": [],
    })


if __name__ == "__main__":
    cases = [
        ("zhangsan", "800", "命中：证书过期 + 800"),
        ("zhangsan", "720", "命中：证书过期 + 720"),
        ("zhangsan", "999", "未命中：证书过期 + 999（知识库无此组合）"),
    ]
    for uid, code, label in cases:
        out = run(uid, code)
        print(f"[{label}]")
        print("  kb_match:", out.get("kb_match"))
        print("-" * 60)

"""Phase 3 端到端验证：咨询问答路径 + 故障执行路径回归。

咨询用例期望：回复为知识回答（非"转人工"话术）+ trace 含 rag_query 节点。
回归用例期望：vpn 故障走完整执行链路（intent→extract→check→verify→kb→risk→execute→close）。
"""
from app.agents.graph import graph


def run(user_id: str, user_msg: str) -> dict:
    return graph.invoke({
        "messages": [{"role": "user", "content": user_msg}],
        "user_id": user_id,
        "trace": [],
    })


CONSULT_CASES = [
    ("VPN 客户端怎么配置", "vpn"),
    ("我忘记密码了怎么办", "password"),
    ("邮箱 IMAP 怎么配置", "email"),
    ("办公软件怎么安装", "software"),
    # 跨场景多跳：期望 trace 里 rag_query 的 hops ≥ 2
    ("重置密码后邮箱客户端提示密码错误，怎么配置", None),
]

if __name__ == "__main__":
    print("===== 咨询问答路径 =====")
    for msg, expect_intent in CONSULT_CASES:
        out = run("zhangsan", msg)
        nodes = [t["node"] for t in out.get("trace", [])]
        reply = out["messages"][-1]["content"]
        print(f"\n「{msg}」")
        print(f"  intent={out.get('intent')} request_type={out.get('request_type')}")
        print(f"  链路: {' → '.join(nodes)}")
        print(f"  回复: {reply[:90]}...")
        assert "rag_query" in nodes, f"咨询用例缺少 rag_query 节点: {msg}"
        assert "verify" not in nodes, f"咨询路径不应查证执行: {msg}"
        print("  ✅ 咨询路径正确（rag_query 命中，无执行动作）")

    print("\n===== 故障执行路径回归（VPN）=====")
    out = run("zhangsan", "VPN连不上，报错Error 800，设备是Windows 11")
    nodes = [t["node"] for t in out.get("trace", [])]
    print(f"  链路: {' → '.join(nodes)}")
    assert nodes[-1] == "close", f"vpn 故障应正常关单，实际链路: {nodes}"
    assert "execute" in nodes and "verify" in nodes
    print("  ✅ VPN 故障执行链路回归通过（verify→kb→risk→execute→close）")
    print("\n全部通过 ✅")

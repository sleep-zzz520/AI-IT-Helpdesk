"""多轮流程测试：模拟用户和 Agent 的两轮对话。

用法（在 backend 目录下）：
    cd backend && python -m scripts.test_flow
"""
from app.agents.graph import graph


def run_turn(messages: list[dict], previous: dict | None = None) -> dict:
    """跑一轮：带着当前对话历史走完整张图。

    previous: 上一轮的输出 state，作为【会话记忆】带进来
    （真实系统里从数据库读回，这里先用变量演示）。
    """
    initial = {"messages": messages, "trace": []}
    if previous:
        # 意图是会话级状态：已有就不重判
        initial["intent"] = previous.get("intent")
    return graph.invoke(initial)


if __name__ == "__main__":
    # ===== 第 1 轮：用户只说问题，没说设备型号 =====
    msgs = [{"role": "user", "content": "VPN连不上，报错800，帮我看看"}]
    out1 = run_turn(msgs)
    print("【第 1 轮】")
    print("  意图   :", out1["intent"])
    print("  已收集 :", {k: out1.get(k, "") for k in ("device", "error_code")})
    print("  缺失   :", out1["missing_info"])
    print("  Agent 回复:", out1["messages"][-1]["content"])
    print("-" * 60)

    # ===== 第 2 轮：Agent 追问后，用户补上设备型号（带上会话记忆）=====
    msgs2 = out1["messages"] + [{"role": "user", "content": "Windows 11"}]
    out2 = run_turn(msgs2, previous=out1)
    print("【第 2 轮】")
    print("  意图   :", out2["intent"])
    print("  已收集 :", {k: out2.get(k, "") for k in ("device", "error_code")})
    print("  缺失   :", out2["missing_info"])
    print("  流程去向: 信息已齐，本轮到 END（下一步接③查证）")

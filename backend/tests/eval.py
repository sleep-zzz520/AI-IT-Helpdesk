"""Eval 自动化评估：意图识别准确率。

用法（在 backend 目录下）：
    cd backend && python -m tests.eval

输出：准确率 + 错例明细 + GLM 调用次数，并保存报告到 backend/tests/report.json
（README 可直接引用报告数据，不伪造跑分）

设计说明：
- 只测 intent 节点（单节点评估），不走整图 → 每例仅 1 次 GLM 调用，聚焦被测对象
- 用例间 sleep 防免费模型限流（429）
"""
import json
import time

from app import llm
from app.agents.nodes.intent import intent_node
from tests.cases import INTENT_CASES

SLEEP_SECONDS = 2  # 防 429 限流


def run_eval() -> dict:
    correct, wrong = 0, []
    results = []

    for i, (message, expected_intent, expected_type) in enumerate(INTENT_CASES):
        try:
            out = intent_node({"messages": [{"role": "user", "content": message}], "trace": []})
            predicted = (out.get("intent"), out.get("request_type"))
        except Exception as e:  # 模型限流/异常按错误计
            predicted = (f"ERROR: {type(e).__name__}", "")
        expected = (expected_intent, expected_type)

        ok = predicted == expected
        correct += ok
        if not ok:
            wrong.append({"message": message, "expected": list(expected), "predicted": list(predicted)})
        results.append({"message": message, "expected": list(expected),
                        "predicted": list(predicted), "ok": ok})
        print(f"  {'✅' if ok else '❌'} {message[:24]:<26} → {predicted} (期望 {expected})")

        if i < len(INTENT_CASES) - 1:
            time.sleep(SLEEP_SECONDS)  # 防限流

    total = len(INTENT_CASES)
    report = {
        "metric": "intent_request_accuracy",
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4),
        "glm_calls": llm.CALL_COUNT,  # 模块属性实时读取（from x import 只会拷贝旧值）
        "wrong": wrong,
        "results": results,
    }
    with open("tests/report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


if __name__ == "__main__":
    print(f"=== 意图识别 Eval（{len(INTENT_CASES)} 条合成用例）===\n")
    r = run_eval()
    print("\n=== 结果 ===")
    print(f"准确率 : {r['correct']}/{r['total']} = {r['accuracy']:.1%}")
    print(f"GLM 调用: {llm.CALL_COUNT} 次")
    if r["wrong"]:
        print(f"\n错例（{len(r['wrong'])} 条）：")
        for w in r["wrong"]:
            print(f"  「{w['message']}」→ 预测 {w['predicted']}，期望 {w['expected']}")
    print("\n报告已保存: tests/report.json")

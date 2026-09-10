"""Eval 自动化评估：意图识别准确率。

用法（在 backend 目录下）：
    cd backend && python -m tests.eval

输出：准确率 + 错例明细 + GLM 调用次数，并保存报告到 backend/tests/report.json
（README 可直接引用报告数据，不伪造跑分）

设计说明：
- 只测 intent 节点（单节点评估），不走整图 → 每例仅 1 次 GLM 调用，聚焦被测对象
- 用例间 sleep 防免费模型限流（429）
- 2026-08-12 升级：用例支持 4 元组（含期望 multi_scenarios），兼容旧 3 元组
"""
import json
import time

from app import llm
from app.agents.nodes.intent import intent_node
from tests.cases import INTENT_CASES

SLEEP_SECONDS = 2  # 防 429 限流


def _unpack(case: tuple) -> tuple:
    """兼容 3/4 元组用例：multi 期望默认 None。"""
    if len(case) == 4:
        return case
    message, intent, rtype = case
    return message, intent, rtype, None


def run_eval() -> dict:
    correct, wrong = 0, []
    results = []

    for i, raw in enumerate(INTENT_CASES):
        message, expected_intent, expected_type, expected_multi = _unpack(raw)
        try:
            out = intent_node({"messages": [{"role": "user", "content": message}], "trace": []})
            predicted = (out.get("intent"), out.get("request_type"))
            predicted_multi = sorted(out["multi_scenarios"]) if out.get("multi_scenarios") else None
        except Exception as e:  # 模型限流/异常按错误计
            predicted = (f"ERROR: {type(e).__name__}", "")
            predicted_multi = None
        expected = (expected_intent, expected_type)

        # 三维判定：intent + request_type + multi（多问题引导）任一不符即错例
        ok = predicted == expected and predicted_multi == expected_multi
        correct += ok
        if not ok:
            wrong.append({"message": message, "expected": list(expected),
                          "expected_multi": expected_multi, "predicted": list(predicted),
                          "predicted_multi": predicted_multi})
        results.append({"message": message, "expected": list(expected),
                        "expected_multi": expected_multi, "predicted": list(predicted),
                        "predicted_multi": predicted_multi, "ok": ok})
        print(f"  {'✅' if ok else '❌'} {message[:26]:<28} → {predicted}"
              f" multi={predicted_multi} (期望 {expected} multi={expected_multi})")

        if i < len(INTENT_CASES) - 1:
            time.sleep(SLEEP_SECONDS)  # 防限流

    total = len(INTENT_CASES)
    # 分维度统计：intent 对 / request_type 对 / multi 对（诊断"错在哪一维"）
    intent_ok = sum(1 for r in results if r["predicted"][0] == r["expected"][0])
    type_ok = sum(1 for r in results if r["predicted"][1] == r["expected"][1])
    multi_cases = [r for r in results if r["expected_multi"] is not None]
    multi_ok = sum(1 for r in multi_cases if r["predicted_multi"] == r["expected_multi"])
    report = {
        "metric": "intent_request_accuracy",
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4),
        "intent_accuracy": round(intent_ok / total, 4),
        "request_type_accuracy": round(type_ok / total, 4),
        "multi_accuracy": round(multi_ok / len(multi_cases), 4) if multi_cases else None,
        "glm_calls": llm.CALL_COUNT,  # 模块属性实时读取（from x import 只会拷贝旧值）
        "wrong": wrong,
        "results": results,
    }
    with open("tests/report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


if __name__ == "__main__":
    print(f"=== 意图识别 Eval（{len(INTENT_CASES)} 条合成用例，含拼写/口语/硬负例/多问题）===\n")
    r = run_eval()
    print("\n=== 结果 ===")
    print(f"三维全对 : {r['correct']}/{r['total']} = {r['accuracy']:.1%}")
    print(f"intent 正确率 : {r['intent_accuracy']:.1%}  "
          f"request_type 正确率 : {r['request_type_accuracy']:.1%}")
    if r["multi_accuracy"] is not None:
        n_multi = sum(1 for x in r["results"] if x["expected_multi"] is not None)
        print(f"multi 引导正确率 : {r['multi_accuracy']:.1%}（{n_multi} 条多问题用例）")
    print(f"GLM 调用: {llm.CALL_COUNT} 次")
    if r["wrong"]:
        print(f"\n错例（{len(r['wrong'])} 条）——这是评估最有价值的输出：")
        for w in r["wrong"]:
            print(f"  「{w['message']}」→ 预测 intent={w['predicted']} multi={w['predicted_multi']}"
                  f"，期望 {w['expected']} multi={w['expected_multi']}")
    print("\n报告已保存: tests/report.json")

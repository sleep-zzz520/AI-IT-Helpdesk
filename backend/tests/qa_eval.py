"""回答质量评估（RAGAS 风格）：faithfulness + answer_relevancy，GLM 免费做裁判。

为什么这两个指标、为什么无需参考答案（RAGAS 核心方法论）：
- faithfulness（忠实性）：回答中的每条声明能否被检索证据支撑。
  做法：LLM 把回答拆成独立声明 → LLM 逐条对照证据判"支撑/不支撑" → 支撑比例。
  防止"模型脑补"——回答再漂亮，不来自证据就是 0 分。
- answer_relevancy（相关性）：回答是否切题。
  做法：LLM 基于回答反推 N 个"用户可能问的问题" → 与原问题 embedding 余弦相似度均值。
  回答牛头不对马嘴时，反推的问题与原问题相似度低。

两者都无需人工参考答案——LLM-as-judge（免费 GLM）+ 向量相似度，纯自动可复现。

用法（backend 目录）：python -m tests.qa_eval
输出：tests/qa_report.json
"""
import json
import time
from pathlib import Path

import numpy as np

from app.agents.nodes.rag_query import answer_question
from app.llm import chat_json
from app.rag.embedder import embed_text

SLEEP_SECONDS = 2  # 防 429 限流（免费模型）

# 评估用例：10 条咨询问题（含跨场景多跳）
QA_CASES = [
    {"query": "VPN 客户端怎么配置", "scenario": "vpn"},
    {"query": "VPN 证书过期了怎么办", "scenario": "vpn"},
    {"query": "VPN 连接报错 720 是什么问题", "scenario": "vpn"},
    {"query": "我忘记密码了怎么重置", "scenario": "password"},
    {"query": "账号被锁定了怎么解锁", "scenario": "password"},
    {"query": "邮箱 IMAP 服务器怎么配置", "scenario": "email"},
    {"query": "邮件发不出去 SMTP 报错 553", "scenario": "email"},
    {"query": "办公软件安装失败怎么办", "scenario": "software"},
    {"query": "软件提示许可证无效怎么激活", "scenario": "software"},
    {"query": "重置密码后邮箱客户端提示密码错误，怎么配置", "scenario": None},
]

REPORT_PATH = Path(__file__).resolve().parent / "qa_report.json"


def _cosine(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def faithfulness(answer: str, evidence_texts: list[str]) -> dict:
    """忠实性：拆声明 → 逐条判据 → 支撑比例。"""
    claims = chat_json([
        {"role": "system", "content":
         "把下面的回答拆成独立的、可验证的事实声明（每句一个声明）。"
         '只输出 JSON：{"claims": ["..."]}'},
        {"role": "user", "content": answer},
    ]).get("claims", [])
    if not claims:
        return {"score": 0.0, "claims": [], "verdicts": [], "reason": "无法拆解声明"}

    context = "\n\n".join(f"[证据{i + 1}] {t}" for i, t in enumerate(evidence_texts))
    verdicts = chat_json([
        {"role": "system", "content":
         f"判断以下每条声明能否被知识库证据支持。证据：\n{context}\n"
         '只输出 JSON：{"verdicts": [{"claim": "...", "supported": true/false, "reason": "..."}]}'},
        {"role": "user", "content": json.dumps(claims, ensure_ascii=False)},
    ]).get("verdicts", [])
    supported = sum(1 for v in verdicts if v.get("supported"))
    total = len(verdicts) or 1
    return {"score": round(supported / total, 3), "claims": claims,
            "verdicts": verdicts, "supported": supported, "total": total}


def answer_relevancy(query: str, answer: str) -> dict:
    """相关性：回答反推问题 → 与原问题 embedding 相似度均值。"""
    questions = chat_json([
        {"role": "system", "content":
         "基于下面的回答，反推 3 个用户最可能问过的问题（覆盖回答的各个部分）。"
         '只输出 JSON：{"questions": ["...", "...", "..."]}'},
        {"role": "user", "content": answer},
    ]).get("questions", [])
    if not questions:
        return {"score": 0.0, "questions": [], "similarities": []}
    q_emb = embed_text(query)
    sims = [_cosine(q_emb, embed_text(q)) for q in questions]
    return {"score": round(float(np.mean(sims)), 3),
            "questions": questions, "similarities": [round(s, 3) for s in sims]}


def evaluate() -> dict:
    results = []
    for i, case in enumerate(QA_CASES):
        print(f"[{i + 1}/{len(QA_CASES)}] {case['query'][:28]}")
        r = answer_question(case["query"], case.get("scenario"))
        ev_texts = [e["text"] for e in r["evidence"]]
        f = faithfulness(r["answer"], ev_texts)
        ar = answer_relevancy(case["query"], r["answer"])
        results.append({
            "query": case["query"],
            "hops": len(r["hops"]),
            "sources": sorted({e["source"].split("/")[-1] for e in r["evidence"]}),
            "faithfulness": f, "answer_relevancy": ar,
        })
        print(f"  hops={len(r['hops'])} faithfulness={f['score']} "
              f"answer_relevancy={ar['score']}")
        if i < len(QA_CASES) - 1:
            time.sleep(SLEEP_SECONDS)

    n = len(results)
    report = {
        "metric": "answer_quality",
        "cases": n,
        "faithfulness_avg": round(sum(x["faithfulness"]["score"] for x in results) / n, 3),
        "answer_relevancy_avg": round(sum(x["answer_relevancy"]["score"] for x in results) / n, 3),
        "multihop_cases": sum(1 for x in results if x["hops"] > 1),
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print("=== RAGAS 风格回答质量评估（10 条咨询用例）===\n")
    r = evaluate()
    print(f"\n=== 结果 ===")
    print(f"faithfulness（忠实性）    : {r['faithfulness_avg']}")
    print(f"answer_relevancy（相关性）: {r['answer_relevancy_avg']}")
    print(f"多跳用例数: {r['multihop_cases']}/{r['cases']}")
    print(f"\n报告已保存: {REPORT_PATH}")

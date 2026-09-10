"""RAG 端到端评测：Gold Chunk、Gold Answer、澄清与拒答。

它和 ``rag_eval.py`` 共享同一份标注数据：
- Gold Chunk：检查回答前是否召回了标准证据；
- Gold Answer：用关键事实覆盖率检查回答是否漏答；
- answerable / expected_behavior：分别统计该回答、澄清或拒答时的行为正确率；
- faithfulness / answer_relevancy：继续保留原有 RAGAS 风格的证据忠实度与切题度。

用法（backend 目录）：python -m tests.qa_eval
输出：tests/qa_report.json
"""
import json
import time
from pathlib import Path

import numpy as np

from app.agents.nodes.rag_query import answer_question
from app.config import settings
from app.llm import chat_json
from app.rag.embedder import embed_text
from tests.rag_eval_dataset import E2E_CASES, dataset_summary, resolve_gold_chunk_ids

SLEEP_SECONDS = 2  # 防 429 限流（免费模型）
REPORT_PATH = Path(__file__).resolve().parent / "qa_report.json"


def _cosine(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def faithfulness(answer: str, evidence_texts: list[str]) -> dict:
    """回答中的每条事实能否由已召回证据支持。"""
    claims = chat_json([
        {"role": "system", "content":
         "把下面的回答拆成独立的、可验证的事实声明（每句一个声明）。"
         '只输出 JSON：{"claims": ["..."]}'},
        {"role": "user", "content": answer},
    ]).get("claims", [])
    if not claims:
        return {"score": 0.0, "claims": [], "verdicts": [], "reason": "无法拆解声明"}

    context = "\n\n".join(f"[证据{i + 1}] {text}" for i, text in enumerate(evidence_texts))
    verdicts = chat_json([
        {"role": "system", "content":
         f"判断以下每条声明能否被知识库证据支持。证据：\n{context}\n"
         '只输出 JSON：{"verdicts": [{"claim": "...", "supported": true/false, "reason": "..."}]}'},
        {"role": "user", "content": json.dumps(claims, ensure_ascii=False)},
    ]).get("verdicts", [])
    verdicts = [verdict for verdict in verdicts if isinstance(verdict, dict)]
    supported = sum(1 for verdict in verdicts if verdict.get("supported") is True)
    total = len(verdicts) or 1
    return {
        "score": round(supported / total, 3),
        "claims": claims,
        "verdicts": verdicts,
        "supported": supported,
        "total": total,
    }


def answer_relevancy(query: str, answer: str) -> dict:
    """回答反推的问题与原问题的向量相似度，衡量是否答非所问。"""
    questions = chat_json([
        {"role": "system", "content":
         "基于下面的回答，反推 3 个用户最可能问过的问题（覆盖回答的各个部分）。"
         '只输出 JSON：{"questions": ["...", "...", "..."]}'},
        {"role": "user", "content": answer},
    ]).get("questions", [])
    questions = [question for question in questions if isinstance(question, str) and question.strip()]
    if not questions:
        return {"score": 0.0, "questions": [], "similarities": []}
    query_embedding = embed_text(query)
    similarities = [_cosine(query_embedding, embed_text(question)) for question in questions]
    return {
        "score": round(float(np.mean(similarities)), 3),
        "questions": questions,
        "similarities": [round(similarity, 3) for similarity in similarities],
    }


def gold_answer_coverage(answer: str, gold_terms: tuple[str, ...]) -> dict:
    """用人工标注的关键事实做可复现的生成覆盖率检查。

    这是“参考答案的最低覆盖要求”，不把词面匹配伪装成完整语义正确率；
    与 faithfulness 一起看，才能区分“有依据但漏答”和“答了但无依据”。
    """
    normalized_answer = answer.replace(" ", "")
    matched = [term for term in gold_terms if term.replace(" ", "") in normalized_answer]
    total = len(gold_terms) or 1
    return {"score": round(len(matched) / total, 3), "matched": matched, "expected": list(gold_terms)}


def evaluate() -> dict:
    gold_ids_by_case = resolve_gold_chunk_ids(E2E_CASES, Path(settings.KB_ROOT))
    results = []
    for index, case in enumerate(E2E_CASES, start=1):
        print(f"[{index}/{len(E2E_CASES)}] {case.case_id} {case.query[:28]}")
        result = answer_question(case.query, case.scenario)
        evidence_ids = {evidence["id"] for evidence in result["evidence"]}
        gold_ids = gold_ids_by_case[case.case_id]
        chunk_coverage = (
            None if not gold_ids else round(len(evidence_ids & gold_ids) / len(gold_ids), 3)
        )
        answer_coverage = gold_answer_coverage(result["answer"], case.gold_answer_terms)

        # 无答案和信息不足题不存在“模型回答事实是否被证据支持”的前提，
        # 只评行为和 Gold Answer 关键要求；可回答题继续跑 RAGAS 风格指标。
        if case.answerable:
            evidence_texts = [evidence["text"] for evidence in result["evidence"]]
            faithful = faithfulness(result["answer"], evidence_texts)
            relevant = answer_relevancy(case.query, result["answer"])
        else:
            faithful = None
            relevant = None

        row = {
            "case_id": case.case_id,
            "query": case.query,
            "question_type": case.question_type,
            "difficulty": case.difficulty,
            "answerable": case.answerable,
            "expected_behavior": case.expected_behavior,
            "expected_min_hops": case.expected_min_hops,
            "hops": len(result["hops"]),
            "gold_chunks": [f"{chunk.source_url}#{chunk.section}" for chunk in case.gold_chunks],
            "gold_chunk_ids": sorted(gold_ids),
            "gold_chunk_coverage": chunk_coverage,
            "gold_answer": case.gold_answer,
            "gold_answer_coverage": answer_coverage,
            "answer": result["answer"],
            "sources": sorted({evidence["source"].split("/")[-1] for evidence in result["evidence"]}),
            "faithfulness": faithful,
            "answer_relevancy": relevant,
        }
        results.append(row)
        print(
            f"  hops={row['hops']} GoldChunk={chunk_coverage} "
            f"GoldAnswer={answer_coverage['score']}"
        )
        if index < len(E2E_CASES):
            time.sleep(SLEEP_SECONDS)

    answerable_rows = [row for row in results if row["answerable"]]
    refusal_rows = [row for row in results if row["expected_behavior"] == "refuse"]
    clarify_rows = [row for row in results if row["expected_behavior"] == "clarify"]
    expected_multihop_rows = [row for row in results if row["expected_min_hops"] > 1]

    def average(rows: list[dict], key: str) -> float | None:
        values = [row[key] for row in rows if row[key] is not None]
        return round(sum(values) / len(values), 3) if values else None

    report = {
        "metric": "rag_end_to_end_gold_eval",
        "cases": len(results),
        "dataset": dataset_summary(E2E_CASES),
        "gold_chunk_coverage_avg": average(answerable_rows, "gold_chunk_coverage"),
        "gold_answer_coverage_avg": round(
            sum(row["gold_answer_coverage"]["score"] for row in results) / len(results), 3
        ),
        "faithfulness_avg": round(
            sum(row["faithfulness"]["score"] for row in answerable_rows) / len(answerable_rows), 3
        ),
        "answer_relevancy_avg": round(
            sum(row["answer_relevancy"]["score"] for row in answerable_rows) / len(answerable_rows), 3
        ),
        "refusal_accuracy": average(
            [{"score": row["gold_answer_coverage"]["score"]} for row in refusal_rows], "score"
        ),
        "clarification_accuracy": average(
            [{"score": row["gold_answer_coverage"]["score"]} for row in clarify_rows], "score"
        ),
        "expected_multihop_cases": len(expected_multihop_rows),
        "actual_multihop_cases": sum(row["hops"] >= 2 for row in expected_multihop_rows),
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(f"=== RAG 端到端 Gold 评测（{len(E2E_CASES)} 条）===\n")
    report = evaluate()
    print("\n=== 结果 ===")
    print(f"Gold Chunk 覆盖率      : {report['gold_chunk_coverage_avg']}")
    print(f"Gold Answer 覆盖率     : {report['gold_answer_coverage_avg']}")
    print(f"faithfulness（忠实性） : {report['faithfulness_avg']}")
    print(f"answer_relevancy（切题）: {report['answer_relevancy_avg']}")
    print(f"拒答正确率             : {report['refusal_accuracy']}")
    print(f"澄清正确率             : {report['clarification_accuracy']}")
    print(
        f"预期多跳命中           : {report['actual_multihop_cases']}/"
        f"{report['expected_multihop_cases']}"
    )
    print(f"\n报告已保存: {REPORT_PATH}")

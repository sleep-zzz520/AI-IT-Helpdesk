"""RAG 端到端评测：固定 Gold Answer、行为预期与候选索引隔离。

与 ``rag_eval.py`` 的 Gold Chunk Recall 不同，本脚本会真实执行
``多跳检索 → 证据 judge → 生成回答``，再分别观察：

- Gold Chunk 是否进入生成前证据；
- Gold Answer 的人工关键事实是否至少被覆盖；
- 回答、澄清、拒答三种预期行为是否一致；
- 已回答问题的证据忠实度和切题度。

默认仍评测当前 active collection，保持历史命令兼容。候选库实验必须显式
传 ``--collection kb_docs_candidate``；因此不需要改蓝绿 active 指针。

用法（backend 目录）：
    python -m tests.qa_eval --collection kb_docs_candidate \
        --report-path /tmp/qa_candidate.json
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from app import llm
from app.agents.nodes.rag_query import answer_question
from app.config import settings
from app.rag.embedder import embed_text
from app.rag.store import create_store, list_collections
from tests.rag_eval_dataset import (
    ALL_CASES,
    E2E_CASES,
    RagEvalCase,
    dataset_summary,
    resolve_gold_chunk_ids,
)

DEFAULT_SLEEP_SECONDS = 2.0  # 防 429 限流；CLI 可在已知限流策略下调整
REPORT_PATH = Path(__file__).resolve().parent / "qa_report.json"
_CLARIFY_PREFIXES = ("请补充", "需要补充", "请提供", "请描述", "请确认")
_NO_ANSWER_MARKERS = ("知识库中暂无", "知识库中未提供", "知识库中没有", "暂无相关内容")
_HANDOFF_MARKERS = ("转人工", "联系人工", "人工协助")
# 无答案时不能把模型自己的常识诊断当作知识库结论；出现这些表述应判为拒答失败。
_UNSUPPORTED_REFUSAL_MARKERS = ("可能涉及", "通常需要", "制冷剂", "压缩机", "建议您")


def _cosine(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def faithfulness(answer: str, evidence_texts: list[str]) -> dict:
    """回答中的每条事实能否被本轮实际注入的知识证据支持。"""
    claims = llm.chat_json([
        {"role": "system", "content":
         "把下面的回答拆成独立的、可验证的事实声明（每句一个声明）。"
         '只输出 JSON：{"claims": ["..."]}'},
        {"role": "user", "content": answer},
    ]).get("claims", [])
    claims = [claim for claim in claims if isinstance(claim, str) and claim.strip()]
    if not claims:
        return {"score": 0.0, "claims": [], "verdicts": [], "reason": "无法拆解声明"}

    context = "\n\n".join(f"[证据{i + 1}] {text}" for i, text in enumerate(evidence_texts))
    verdicts = llm.chat_json([
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
    """以“由回答反推的问题”和原问题的向量相似度观察是否答非所问。"""
    questions = llm.chat_json([
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
    """检查人工标注的最低事实要求，不把词面命中当成语义正确。"""
    if not gold_terms:
        return {"score": None, "matched": [], "expected": [], "reason": "该样本未标注关键事实"}
    normalized_answer = answer.replace(" ", "")
    matched = [term for term in gold_terms if term.replace(" ", "") in normalized_answer]
    return {
        "score": round(len(matched) / len(gold_terms), 3),
        "matched": matched,
        "expected": list(gold_terms),
    }


def _behavior_result(case: RagEvalCase, answer: str, evidence: list[dict],
                     answer_coverage: dict, answer_failed: bool) -> dict:
    """按行为类型评分，和 Gold Answer 覆盖率分开保留。

    澄清题不能只因回答末尾含有“请描述故障”就通过：每个缺失字段都必须
    出现在明确的补充请求中。拒答题则必须先说明知识库没有该内容、提供人工
    转交，并且不能在无证据时补写领域常识或维修步骤。
    """
    if answer_failed:
        return {"passed": None, "reason": "回答调用失败，不能判定行为"}

    if case.expected_behavior == "answer":
        refused = "知识库中暂无相关内容" in answer
        passed = bool(evidence) and not refused
        return {
            "passed": passed,
            "reason": "有证据且未落入固定暂无兜底" if passed else "未得到可用知识回答",
        }

    if case.expected_behavior == "clarify":
        request_sentences = [
            sentence for sentence in re.split(r"[。！？!?\n]", answer)
            if any(prefix in sentence for prefix in _CLARIFY_PREFIXES)
        ]
        requested = [
            term for term in case.gold_answer_terms
            if any(term in sentence for sentence in request_sentences)
        ]
        missing = [term for term in case.gold_answer_terms if term not in requested]
        return {
            "passed": not missing,
            "requested_terms": requested,
            "missing_terms": missing,
            "reason": "已明确请求所有必要信息" if not missing else "未明确请求全部必要信息",
        }

    if case.expected_behavior == "refuse":
        has_no_answer = any(marker in answer for marker in _NO_ANSWER_MARKERS)
        has_handoff = any(marker in answer for marker in _HANDOFF_MARKERS)
        unsupported = [marker for marker in _UNSUPPORTED_REFUSAL_MARKERS if marker in answer]
        passed = has_no_answer and has_handoff and not unsupported
        return {
            "passed": passed,
            "has_no_answer_statement": has_no_answer,
            "has_handoff": has_handoff,
            "unsupported_markers": unsupported,
            "reason": "明确拒答并安全转人工" if passed else "拒答措辞或无证据内容不符合要求",
        }

    return {"passed": None, "reason": f"未知预期行为：{case.expected_behavior}"}


def _run_metric(stage: str, action: Callable[[], object], errors: list[dict]) -> object | None:
    """评测指标失败不丢弃已生成答案，也不阻断后续样本。"""
    try:
        return action()
    except Exception as exc:  # 外部模型/Embedding 失败应进入报告而不是静默消失
        errors.append({
            "stage": stage,
            "error_type": type(exc).__name__,
            "message": str(exc)[:300],
        })
        return None


def _average(rows: list[dict], key: str) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    return round(sum(values) / len(values), 3) if values else None


def _behavior_accuracy(rows: list[dict], expected_behavior: str) -> float | None:
    values = [
        row["behavior"]["passed"]
        for row in rows
        if row["expected_behavior"] == expected_behavior
        and row["behavior"]["passed"] is not None
    ]
    return round(sum(values) / len(values), 3) if values else None


def _build_report(*, cases: tuple[RagEvalCase, ...], results: list[dict],
                  store, status: str, started_at: float, initial_llm_calls: int,
                  metric_errors: int) -> dict:
    """供阶段性落盘和最终报告共用，防止长跑只留下半截终端文本。"""
    answerable_rows = [row for row in results if row["answerable"]]
    expected_multihop_rows = [row for row in results if row["expected_min_hops"] > 1]
    return {
        "metric": "rag_end_to_end_gold_eval",
        "status": status,
        "collection": store.collection_name,
        "collection_chunks": store.count(),
        "cases_requested": len(cases),
        "cases_completed": len(results),
        "dataset": dataset_summary(cases),
        "model": {
            "answer_chain": settings.GLM_MODELS,
            "json_chain": settings.GLM_JSON_MODELS,
            "request_timeout_seconds": settings.LLM_REQUEST_TIMEOUT,
            "last_observed_model": llm.LAST_MODEL,
        },
        "retrieval": {
            "rrf_k": settings.RRF_K,
            "vector_weight": settings.RRF_VECTOR_WEIGHT,
            "bm25_weight": settings.RRF_BM25_WEIGHT,
            "recall_n": settings.RRF_RECALL_N,
            "fusion_candidate_n": settings.RRF_FUSION_CANDIDATE_N,
            "max_chunks_per_source": settings.RAG_MAX_CHUNKS_PER_SOURCE,
            "rerank_enabled": settings.RERANK_ENABLED,
        },
        "duration_seconds": round(time.perf_counter() - started_at, 1),
        "llm_calls": llm.CALL_COUNT - initial_llm_calls,
        "metric_error_count": metric_errors,
        "answer_call_error_count": sum(bool(row["answer_error"]) for row in results),
        "gold_chunk_coverage_avg": _average(answerable_rows, "gold_chunk_coverage"),
        "gold_answer_coverage_avg": _average(results, "gold_answer_coverage_score"),
        "gold_answer_coverage_cases": sum(
            row["gold_answer_coverage_score"] is not None for row in results
        ),
        "faithfulness_avg": _average(answerable_rows, "faithfulness_score"),
        "faithfulness_cases": sum(row["faithfulness_score"] is not None for row in answerable_rows),
        "answer_relevancy_avg": _average(answerable_rows, "answer_relevancy_score"),
        "answer_relevancy_cases": sum(
            row["answer_relevancy_score"] is not None for row in answerable_rows
        ),
        "answer_behavior_accuracy": _behavior_accuracy(results, "answer"),
        "refusal_accuracy": _behavior_accuracy(results, "refuse"),
        "clarification_accuracy": _behavior_accuracy(results, "clarify"),
        "expected_multihop_cases": len(expected_multihop_rows),
        "actual_multihop_cases": sum(row["hops"] >= 2 for row in expected_multihop_rows),
        "results": results,
    }


def _write_report(report_path: Path, report: dict) -> None:
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def rescore_behavior(report_path: Path) -> dict:
    """按当前确定性行为规则重算已有报告，不再发起 LLM 或 Embedding 请求。"""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cases_by_id = {case.case_id: case for case in ALL_CASES}
    for row in report.get("results", []):
        case = cases_by_id.get(row["case_id"])
        if case is None:
            raise ValueError(f"报告含未知 case_id：{row['case_id']}")
        row["behavior"] = _behavior_result(
            case,
            row.get("answer", ""),
            [{"source": source} for source in row.get("sources", [])],
            row.get("gold_answer_coverage", {}),
            bool(row.get("answer_error")),
        )
    rows = report.get("results", [])
    report["behavior_rules_version"] = "v2_explicit_clarify_and_safe_refusal"
    report["answer_behavior_accuracy"] = _behavior_accuracy(rows, "answer")
    report["refusal_accuracy"] = _behavior_accuracy(rows, "refuse")
    report["clarification_accuracy"] = _behavior_accuracy(rows, "clarify")
    _write_report(report_path, report)
    return report


def _select_cases(case_set: str, raw_extra_ids: str) -> tuple[RagEvalCase, ...]:
    """基线集合可加定向 Case，且保持数据集原有顺序与去重。"""
    base = E2E_CASES if case_set == "e2e" else ALL_CASES
    extra_ids = [item.strip() for item in raw_extra_ids.split(",") if item.strip()]
    by_id = {case.case_id: case for case in ALL_CASES}
    unknown = [case_id for case_id in extra_ids if case_id not in by_id]
    if unknown:
        raise ValueError(f"未知 case_id：{', '.join(unknown)}")
    # E2E_CASES 是 replace() 后附带 ``gold_answer_terms`` 的对象；不能再从
    # ALL_CASES 按同一 ID 取回原对象，否则会静默丢失人工 Gold Answer 标注。
    chosen = list(base)
    chosen_ids = {case.case_id for case in chosen}
    for case_id in extra_ids:
        if case_id not in chosen_ids:
            chosen.append(by_id[case_id])
            chosen_ids.add(case_id)
    return tuple(chosen)


def evaluate(cases: tuple[RagEvalCase, ...], store, report_path: Path,
             sleep_seconds: float, *, quality_metrics: bool = True,
             resume_results: list[dict] | None = None) -> dict:
    """执行真实候选库问答；每完成一题就更新 JSON，进程中断也可回看进度。"""
    gold_ids_by_case = resolve_gold_chunk_ids(cases, Path(settings.KB_ROOT))
    results: list[dict] = list(resume_results or [])
    completed_ids = {row["case_id"] for row in results}
    if not completed_ids <= {case.case_id for case in cases}:
        raise ValueError("恢复结果包含本次评测之外的 case_id")
    initial_llm_calls = llm.CALL_COUNT
    metric_errors = sum(len(row.get("metric_errors", [])) for row in results)
    started_at = time.perf_counter()

    for index, case in enumerate(cases, start=1):
        if case.case_id in completed_ids:
            continue
        print(f"[{index}/{len(cases)}] {case.case_id} {case.query[:36]}", flush=True)
        errors: list[dict] = []
        result = _run_metric(
            "answer_question",
            lambda: answer_question(
                case.query,
                case.scenario,
                retrieval_store=store,
            ),
            errors,
        )
        answer_failed = result is None
        result = result or {"answer": "", "evidence": [], "hops": []}
        pipeline_errors = [hop["judge"]["error"] for hop in result["hops"]
                           if isinstance(hop.get("judge"), dict) and hop["judge"].get("error")]
        pipeline_errors += [hop["generation"]["error"] for hop in result["hops"]
                            if isinstance(hop.get("generation"), dict) and hop["generation"].get("error")]
        if pipeline_errors:
            answer_failed = True
            errors.append({"stage": "evidence_gate", "error_type": "PipelineFailure"})
        evidence = result["evidence"]
        evidence_ids = {item["id"] for item in evidence}
        gold_ids = gold_ids_by_case[case.case_id]
        chunk_coverage = (
            None if not gold_ids else round(len(evidence_ids & gold_ids) / len(gold_ids), 3)
        )
        answer_coverage = gold_answer_coverage(result["answer"], case.gold_answer_terms)

        if quality_metrics and case.answerable and not answer_failed:
            evidence_texts = [item["text"] for item in evidence]
            faithful = _run_metric(
                "faithfulness",
                lambda: faithfulness(result["answer"], evidence_texts),
                errors,
            )
            relevant = _run_metric(
                "answer_relevancy",
                lambda: answer_relevancy(case.query, result["answer"]),
                errors,
            )
        else:
            faithful = None
            relevant = None

        behavior = _behavior_result(
            case,
            result["answer"],
            evidence,
            answer_coverage,
            answer_failed,
        )
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
            "gold_answer_coverage_score": answer_coverage["score"],
            "behavior": behavior,
            "answer": result["answer"],
            "pipeline_errors": pipeline_errors,
            "quality_metrics_skipped": not quality_metrics,
            "sources": sorted({item["source"].split("/")[-1] for item in evidence}),
            "faithfulness": faithful,
            "faithfulness_score": faithful["score"] if faithful is not None else None,
            "answer_relevancy": relevant,
            "answer_relevancy_score": relevant["score"] if relevant is not None else None,
            "answer_error": errors[0] if answer_failed else None,
            "metric_errors": [] if answer_failed else errors,
        }
        metric_errors += len(row["metric_errors"])
        results.append(row)
        print(
            f"  hops={row['hops']} GoldChunk={chunk_coverage} "
            f"GoldAnswer={answer_coverage['score']} behavior={behavior['passed']} "
            f"metric_errors={len(row['metric_errors'])}",
            flush=True,
        )

        partial = _build_report(
            cases=cases,
            results=results,
            store=store,
            status="running",
            started_at=started_at,
            initial_llm_calls=initial_llm_calls,
            metric_errors=metric_errors,
        )
        _write_report(report_path, partial)
        if index < len(cases) and sleep_seconds:
            time.sleep(sleep_seconds)

    report = _build_report(
        cases=cases,
        results=results,
        store=store,
        status="completed",
        started_at=started_at,
        initial_llm_calls=initial_llm_calls,
        metric_errors=metric_errors,
    )
    _write_report(report_path, report)
    report["quality_metrics_requested"] = quality_metrics
    report["resumed_cases"] = len(resume_results or [])
    _write_report(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 端到端 Gold Answer 评测")
    parser.add_argument(
        "--collection",
        default=settings.active_collection,
        help="已存在的 collection；默认 active，候选实验须显式传入候选库名",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=REPORT_PATH,
        help="报告路径；实验建议传 /tmp 下的新文件，避免覆盖历史基线",
    )
    parser.add_argument(
        "--case-set",
        choices=("e2e", "all"),
        default="e2e",
        help="e2e=29 条已标 Gold Answer 的端到端保留集；all=全部 70 条",
    )
    parser.add_argument(
        "--case-ids",
        default="",
        help="额外加入的逗号分隔 case_id，例如 r57,r29；会自动去重",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help="两题之间的限流间隔，默认 2 秒",
    )
    parser.add_argument(
        "--rescore-behavior",
        action="store_true",
        help="只按当前确定性规则重算既有报告的行为指标，不调用模型或 Embedding",
    )
    parser.add_argument("--skip-quality-metrics", action="store_true",
                        help="只测真实问答行为、Gold证据与答案关键事实；跳过额外LLM质量打分")
    args = parser.parse_args()

    if args.rescore_behavior:
        if not args.report_path.is_file():
            parser.error(f"待重算报告不存在：{args.report_path}")
        try:
            report = rescore_behavior(args.report_path)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"行为指标已重算: {args.report_path}", flush=True)
        print(f"回答行为正确率         : {report['answer_behavior_accuracy']}", flush=True)
        print(f"拒答正确率             : {report['refusal_accuracy']}", flush=True)
        print(f"澄清正确率             : {report['clarification_accuracy']}", flush=True)
        return

    if args.sleep_seconds < 0:
        parser.error("--sleep-seconds 不能小于 0")
    if not args.report_path.parent.is_dir():
        parser.error(f"报告目录不存在：{args.report_path.parent}")
    collections = list_collections(settings.KB_VECTOR_DIR)
    if args.collection not in collections:
        parser.error(f"collection 不存在：{args.collection}")
    if settings.RERANK_ENABLED:
        parser.error("端到端候选库对照要求 RERANK_ENABLED=false；请先关闭后再运行")
    if settings.GLM_MODELS != ["glm-5.3-flash"] or settings.GLM_JSON_MODELS != ["glm-5.3-flash"]:
        parser.error("本次固定模型评测要求 GLM_MODELS 与 GLM_JSON_MODELS 都是 glm-5.3-flash")
    try:
        cases = _select_cases(args.case_set, args.case_ids)
    except ValueError as exc:
        parser.error(str(exc))
    store = create_store(collection_name=args.collection)

    print(f"=== RAG 端到端 Gold 评测（{len(cases)} 条）===", flush=True)
    print(
        f"collection={store.collection_name} chunks={store.count()} | "
        f"model=glm-5.3-flash | rerank={settings.RERANK_ENABLED}",
        flush=True,
    )
    report = evaluate(cases, store, args.report_path, args.sleep_seconds,
                      quality_metrics=not args.skip_quality_metrics)
    print("\n=== 结果 ===", flush=True)
    print(f"Gold Chunk 覆盖率      : {report['gold_chunk_coverage_avg']}", flush=True)
    print(f"Gold Answer 覆盖率     : {report['gold_answer_coverage_avg']}"
          f"（{report['gold_answer_coverage_cases']} 条有人工关键事实标注）", flush=True)
    print(f"faithfulness（忠实性） : {report['faithfulness_avg']}", flush=True)
    print(f"answer_relevancy（切题）: {report['answer_relevancy_avg']}", flush=True)
    print(f"回答行为正确率         : {report['answer_behavior_accuracy']}", flush=True)
    print(f"拒答正确率             : {report['refusal_accuracy']}", flush=True)
    print(f"澄清正确率             : {report['clarification_accuracy']}", flush=True)
    print(
        f"预期多跳命中           : {report['actual_multihop_cases']}/"
        f"{report['expected_multihop_cases']}",
        flush=True,
    )
    print(
        f"模型调用={report['llm_calls']}，评测指标异常={report['metric_error_count']}，"
        f"回答调用异常={report['answer_call_error_count']}",
        flush=True,
    )
    print(f"报告已保存: {args.report_path}", flush=True)


if __name__ == "__main__":
    main()

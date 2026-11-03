"""Run the versioned, synthetic RAG release suite without touching the active KB."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.rag_eval_scoring import score_answer
from scripts.rag_quality_metrics import quality_metrics

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/rag_v1.json"
MODES = ("retrieval", "oracle", "e2e")
APPROVED_JUDGE = {"enough": True, "answerable": True, "executable": False,
                  "missing_info": [], "next_query": ""}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_fixture(path):
    suite = json.loads(path.read_text())
    if suite["schema_version"] != 1:
        raise ValueError("unsupported fixture schema")
    documents = suite["documents"]
    cases = suite["cases"]
    docs = {doc["id"] for doc in documents}
    if not documents or len(docs) != len(documents) or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("empty snapshot or duplicate IDs")
    families = {split: {c["family"] for c in cases if c["split"] == split}
                for split in ("dev", "frozen")}
    if not all(families.values()) or families["dev"] & families["frozen"]:
        raise ValueError("empty split or leaked question family")
    for case in cases:
        expected = case["expected"]
        gold = set(expected["required_evidence"])
        forbidden = set(expected["forbidden_evidence"])
        facts = expected["facts"]
        if (case["split"] not in families or type(case["critical"]) is not bool
                or gold & forbidden or not (gold | forbidden) <= docs
                or len({fact["id"] for fact in facts}) != len(facts)
                or any(not fact["evidence"] or not set(fact["evidence"]) <= gold for fact in facts)
                or expected["action"] not in {"answer", "clarify", "refuse"}
                or (expected["action"] == "answer" and (not gold or not facts))
                or (expected["action"] == "clarify" and not expected["clarify_patterns"])):
            raise ValueError(f"invalid expectations: {case['id']}")
        for pattern in [*(f["pattern"] for f in facts), *expected["clarify_patterns"],
                        *expected["answer_excludes"], *expected.get("refuse_patterns", []),
                        *(f["evidence_pattern"] for f in facts if "evidence_pattern" in f)]:
            if not pattern or re.compile(pattern).search(""):
                raise ValueError(f"empty matching assertion: {case['id']}")
    return suite


def with_snapshot_permissions(case, documents):
    """Build the evidence deny set independently of the retriever's policy."""
    if case.get("permission") != "employee":
        return case
    expected = case["expected"]
    denied = set(expected["forbidden_evidence"])
    denied.update(doc["id"] for doc in documents
                  if doc["metadata"].get("access", "employee") != "employee")
    return {**case, "expected": {**expected, "forbidden_evidence": sorted(denied)}}


def score_output(case, row):
    context = row["injected"]
    if case["expected"]["action"] == "clarify" and not context:
        context = [item for hop in row.get("judge_inputs", []) for item in hop]
    return score_answer(case, row["answer"], context)


def leaked_evidence(case, row):
    observed = [*row.get("candidate_evidence", []), *row.get("injected", []),
                *(item for hop in row.get("judge_inputs", []) for item in hop)]
    denied = set(case["expected"]["forbidden_evidence"])
    return sorted(denied & {item["document_id"] for item in observed})


def planned(suite, split):
    for case in suite["cases"]:
        if case["split"] == split:
            for mode in MODES:
                if mode != "oracle" or case["expected"]["action"] == "answer":
                    yield with_snapshot_permissions(case, suite.get("documents", [])), mode


def score_retrieval(case, ids):
    gold = set(case["expected"]["required_evidence"])
    forbidden = set(ids) & set(case["expected"]["forbidden_evidence"])
    ranks = [rank for rank, doc in enumerate(ids, 1) if doc in gold]
    return {"recall": len(gold & set(ids)) / len(gold) if gold else None,
            "mrr": (1 / ranks[0] if ranks else 0.0) if gold else None,
            "all_gold": gold <= set(ids) if gold else None,
            "forbidden": sorted(forbidden),
            "passed": not forbidden and gold <= set(ids)}


def release_errors(report, suite):
    expected = {(c["id"], mode): c for c, mode in planned(suite, "frozen")}
    if (report.get("split") != "frozen" or report.get("suite_sha256") != digest(suite)
            or report.get("status") != "completed"):
        return ["incomplete or mismatched frozen run"]
    errors = []
    seen = set()
    for row in report.get("rows", []):
        key = row.get("case_id"), row.get("mode")
        if key in seen or key not in expected:
            errors.append(f"unexpected or duplicate row {key}")
        seen.add(key)
        if key not in expected:
            continue
        try:
            if key[1] == "retrieval":
                ids = [hit["document_id"] for hit in row["retrieval_calls"][0]["hits"]]
                score = score_retrieval(expected[key], ids)
            else:
                score = score_output(expected[key], row)
                if leaked_evidence(expected[key], row):
                    errors.append(f"{key}: forbidden evidence")
            if (row["errors"] != [] or row["score"] != score
                    or score["passed"] is not True):
                errors.append(f"{key}: invalid score, quality failure or dependency failure")
        except (KeyError, IndexError, TypeError, ValueError):
            errors.append(f"{key}: missing or malformed evidence/score")
    errors.extend(f"missing row {key}" for key in sorted(expected.keys() - seen))
    return errors


def write_report(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


class Meter:
    """Wrap the SDK call in the isolated worker to observe failures and usage."""
    def __init__(self, create, kind, events):
        self.create = create
        self.kind = kind
        self.events = events

    def __call__(self, **kwargs):
        event = {"kind": self.kind, "model": kwargs["model"]}
        started = time.monotonic()
        try:
            response = self.create(**kwargs)
            usage = getattr(response, "usage", None)
            event["usage"] = {k: getattr(usage, k, None) for k in
                              ("prompt_tokens", "completion_tokens", "total_tokens")}
            event["response_model"] = getattr(response, "model", None)
            if self.kind == "chat" and response.choices:
                event["finish_reason"] = response.choices[0].finish_reason
            return response
        except Exception as exc:
            event["error"] = type(exc).__name__
            event["status_code"] = getattr(exc, "status_code", None)
            raise
        finally:
            event["seconds"] = round(time.monotonic() - started, 3)
            self.events.append(event)


def run_worker(root, suite, split, report):
    from app import llm
    from app.agents.nodes import rag_query as rag
    from app.config import settings
    from app.rag import embedder
    from app.rag.chunks import KnowledgeDoc
    from app.rag.retriever import RetrievalStats, retrieve
    from app.rag.splitter import split_markdown
    from app.rag.store import ChromaStore

    if not settings.ZHIPU_API_KEY or settings.ZHIPU_API_KEY == "ci-placeholder":
        raise ValueError("missing real model credential")
    # Keep the configured model chains and retrieval settings. Isolate policy state
    # and bound network retries in this worker only.
    settings.KB_SYNC_MODE = "legacy"
    settings.KB_ACTIVE_FILE = root / "unused_active_pointer"
    settings.KB_SYNC_STATE_DIR = str(root / "unused_sync_state")
    report["configuration"] = {
        "answer_models": settings.GLM_MODELS, "judge_models": settings.GLM_JSON_MODELS,
        "embedding_model": embedder.EMBED_MODEL, "embedding_dimensions": embedder.EMBED_DIM,
        "retrieval": {key: getattr(settings, key) for key in (
            "RRF_K", "RRF_VECTOR_WEIGHT", "RRF_BM25_WEIGHT", "RRF_RECALL_N",
            "RRF_FUSION_CANDIDATE_N", "RAG_MAX_CHUNKS_PER_SOURCE", "RERANK_ENABLED")},
        "hops": rag.MAX_HOPS, "top_k": rag.HOP_TOP_K, "request_timeout_seconds": 25,
        "per_stage_seconds": 80, "concurrency": 1, "policy": "synthetic snapshot, no live ACL",
    }
    llm_client = llm.client.with_options(timeout=25, max_retries=0)
    embedding_client = embedder._client.with_options(timeout=25, max_retries=0)
    events = []
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=Meter(llm_client.chat.completions.create, "chat", events))))
    embedder._client = SimpleNamespace(embeddings=SimpleNamespace(
        create=Meter(embedding_client.embeddings.create, "embedding", events)))

    chunks, oracle = [], {}
    for doc in suite["documents"]:
        meta = dict(doc["metadata"])
        source = meta.get("source_url") or meta.get("source") or f"{doc['id']}.md"
        meta["source_url"] = source
        parts = split_markdown(KnowledgeDoc(source, doc["id"], meta, doc["text"]))
        chunks.extend([parts.parent, *parts.children])
        oracle[doc["id"]] = {
            "document_id": doc["id"], "id": parts.parent.id,
            "source": source, "text": parts.parent.text[:rag.EVIDENCE_MAX_CHARS],
        }
    child_ids = {chunk.id for chunk in chunks if chunk.metadata.get("chunk_type") == "child"}
    for case in suite["cases"]:
        expected = case["expected"]
        gold_chunks = expected.get("relevant_chunks", [])
        if (len(gold_chunks) != len(set(gold_chunks)) or not set(gold_chunks) <= child_ids
                or (expected.get("relevance_complete") is True and "relevant_chunks" not in expected)):
            raise ValueError(f"invalid child relevance labels: {case['id']}")
    report["stage"] = "index"
    report["index_calls"] = events.copy()
    write_report(root / "report.json", report)
    try:
        with embedder.single_attempt():
            vectors = embedder.embed_texts([chunk.text for chunk in chunks])
        store = ChromaStore(str(root / "chroma"), "rag_release_isolated")
        store.upsert_chunks(chunks, vectors)
    finally:
        report["index_calls"] = events.copy()

    def deadline(_signal, _frame):
        raise TimeoutError("stage deadline")

    signal.signal(signal.SIGALRM, deadline)
    report["stage"] = "cases"
    for case, mode in planned(suite, split):
        events.clear()
        injected, retrieval_calls, judge_inputs = [], [], []
        row = {"case_id": case["id"], "family": case["family"], "mode": mode,
               "critical": case["critical"], "question": case["question"],
               "environment": case["environment"], "permission": case["permission"],
               "expected": case["expected"], "errors": [], "score": None}
        report["current"] = {"case_id": case["id"], "mode": mode}
        write_report(root / "report.json", report)
        scenario = case.get("filters", {}).get("scenario")

        def trace_retrieve(query, **kwargs):
            stats = RetrievalStats()
            with embedder.single_attempt():
                hits = retrieve(query, stats=stats, **kwargs)
            retrieval_calls.append({"query": query, "scenario": kwargs.get("scenario"),
                                    "notes": stats.notes, "hits": [
                {"chunk_id": h.id, "document_id": h.metadata["doc_id"],
                 "score": h.score, "routes": sorted(h.routes)} for h in hits]})
            if any("失败" in note or "降级" in note for note in stats.notes):
                row["errors"].append({"stage": "retrieval", "type": "degraded"})
            return hits

        generate = rag.generate_answer
        judge_evidence = rag.judge_evidence

        def trace_judge(query, evidence, *args):
            judge_inputs.append([dict(item) for item in evidence])
            return judge_evidence(query, evidence, *args)

        def trace_generate(query, evidence, judge, *args):
            injected.extend(dict(item) for item in evidence)
            return generate(query, evidence, judge, *args)

        start = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, 80)
        try:
            if mode == "retrieval":
                hits = trace_retrieve(case["question"], scenario=scenario, store=store,
                                      top_k=rag.HOP_TOP_K)
                row["score"] = score_retrieval(case, [h.metadata["doc_id"] for h in hits])
            else:
                if mode == "oracle":
                    evidence = [oracle[doc] for doc in case["expected"]["required_evidence"]]
                    answer = trace_generate(case["question"], evidence, APPROVED_JUDGE)
                else:
                    with patch.object(rag, "retrieve", trace_retrieve), patch.object(
                        rag, "generate_answer", trace_generate
                    ), patch.object(rag, "judge_evidence", trace_judge):
                        result = rag.answer_question(case["question"], scenario=scenario,
                                                     retrieval_store=store)
                    answer = result["answer"]
                    row["hops"] = result["hops"]
                    row["candidate_evidence"] = result["evidence"]
                    for hop in row["hops"]:
                        judge = hop.get("judge", {})
                        if "error" in judge:
                            reason = judge.pop("error")
                            stage = "retrieval" if reason.startswith("检索失败") else "product_judge"
                            error_type = judge.get("gate", "call_failed")
                            if reason.startswith("judge 失败: 模型未返回合法 JSON"):
                                error_type = "invalid_json"
                            judge["error"] = error_type
                            row["errors"].append({"stage": stage, "type": error_type})
                        if "error" in hop.get("generation", {}):
                            row["errors"].append({"stage": "generation", "type": hop["generation"]["error"]})
                row["answer"] = answer
                row.update(injected=injected, judge_inputs=judge_inputs)
                row["score"] = score_output(case, row)
                leaked = leaked_evidence(case, row)
                if leaked:
                    row["errors"].append({"stage": "evidence_gate", "forbidden": leaked})
        except Exception as exc:
            row["errors"].append({"stage": mode, "type": type(exc).__name__})
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        row["errors"].extend({"stage": event["kind"], "type": event["error"]}
                             for event in events if "error" in event)
        row.update(seconds=round(time.monotonic() - start, 3), calls=events.copy(),
                   injected=injected, retrieval_calls=retrieval_calls, judge_inputs=judge_inputs)
        report["rows"].append(row)
        write_report(root / "report.json", report)
        time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "frozen"), default="frozen")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--fixture", type=Path, default=FIXTURE,
                        help="versioned synthetic or reviewed API-safe snapshot and questions")
    parser.add_argument("--budget-seconds", type=int, choices=range(1, 901), default=600,
                        metavar="1..900")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    suite = load_fixture(args.fixture)
    root = args.output_dir or Path(tempfile.mkdtemp(prefix="it-rag-release-"))
    root = root.resolve()
    path = root / "report.json"
    if args.worker:
        report = json.loads(path.read_text())
        try:
            run_worker(root, suite, args.split, report)
            report["status"] = "completed"
            report.pop("current", None)
        except Exception as exc:
            report.update(status="failed", error_type=type(exc).__name__)
        write_report(path, report)
        return 0 if report["status"] == "completed" else 1

    if args.output_dir:
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
    plan = list(planned(suite, args.split))
    started = time.monotonic()
    report = {
        "split": args.split, "suite_sha256": digest(suite),
        "snapshot_sha256": digest(suite["documents"]),
        "dataset_version": suite["dataset_version"], "knowledge_version": suite["knowledge_version"],
        "status": "running", "rows": [],
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_worktree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"])),
        "budget_seconds": args.budget_seconds, "stage": "startup",
    }
    write_report(path, report)
    env = {**os.environ, "ANONYMIZED_TELEMETRY": "False",
           "KB_VECTOR_DIR": str(root / "chroma"), "KB_ROOT": str(root / "unused_kb"),
           "KB_SYNC_MODE": "legacy", "KB_SYNC_STATE_DIR": str(root / "unused_sync_state")}
    command = [sys.executable, "-m", "scripts.eval_rag_release", "--worker",
               "--split", args.split, "--output-dir", str(root),
               "--fixture", str(args.fixture.resolve())]
    with (root / "worker.log").open("w") as log:
        try:
            result = subprocess.run(command, env=env, timeout=args.budget_seconds,
                                    stdout=log, stderr=log, check=False)
            status = "worker_failed" if result.returncode else None
        except subprocess.TimeoutExpired:
            status = "budget_exhausted"
    report = json.loads(path.read_text())
    if status and report["status"] == "running":
        report["status"] = status
    report["seconds"] = round(time.monotonic() - started, 3)
    report["summary"] = {}
    rows = {(r["case_id"], r["mode"]): r for r in report["rows"]}
    for mode in MODES:
        ids = [c["id"] for c, m in plan if m == mode]
        passed = [cid for cid in ids if (r := rows.get((cid, mode))) and not r["errors"]
                  and (r["score"] or {}).get("passed") is True]
        report["summary"][mode] = {"passed": len(passed), "total": len(ids),
                                   "failed_or_missing": [cid for cid in ids if cid not in passed]}
    answer_cases = [c for c, mode in plan if mode == "e2e"]
    report["behavior"] = {}
    for action in ("answer", "clarify", "refuse"):
        ids = [c["id"] for c in answer_cases if c["expected"]["action"] == action]
        valid = [(rows.get((cid, "e2e"), {}).get("score") or {}) for cid in ids
                 if not rows.get((cid, "e2e"), {}).get("errors")]
        report["behavior"][action] = {
            "passed": sum(s.get("behavior_passed") is True for s in valid), "total": len(ids),
        }
    report["fact_checks"] = {}
    for mode in ("oracle", "e2e"):
        cases = [c for c, m in plan if m == mode]
        scores = [(rows.get((c["id"], mode), {}).get("score") or {}) for c in cases
                  if not rows.get((c["id"], mode), {}).get("errors")]
        report["fact_checks"][mode] = {
            "total": sum(len(c["expected"]["facts"]) for c in cases),
            **{metric: sum(s.get(metric, 0) for s in scores)
               for metric in ("facts_present", "facts_supported", "facts_cited")},
        }
    report["quality_metrics"] = quality_metrics(report, suite)
    report["release_errors"] = release_errors(report, suite) if args.split == "frozen" else None
    write_report(path, report)
    print(json.dumps({"report": str(path), "status": report["status"],
                      "summary": report["summary"]}, ensure_ascii=False))
    return int(report["status"] != "completed" or bool(report["release_errors"]))


if __name__ == "__main__":
    raise SystemExit(main())

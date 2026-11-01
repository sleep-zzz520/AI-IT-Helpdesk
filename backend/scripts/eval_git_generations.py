"""真实MySQL/Chroma增量验收；仅创建隔离source和临时向量目录，结束清理自己的SQL行。

显式 --real-embedding 才调用供应商；否则使用确定性向量做流程烟测。
不切换真实来源，不修改知识文档，不调用回答模型。
"""
import argparse
import hashlib
import json
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete, text

from app import db as db_module
from app.config import BASE_DIR, settings
from app.models import KbSource, KbSyncJob
from app.rag import embedder, sync
from app.rag import generations as g
from app.rag.chunks import KnowledgeDoc
from app.rag.git_source import read_snapshot, read_snapshot_from_files
from app.rag.retriever import retrieve
from app.rag.store import ChromaStore, create_store
from tests.rag_eval import _evaluate
from tests.rag_eval_dataset import RETRIEVAL_CASES


def gold_for(store):
    all_meta = store.get_all_meta()
    result = {}
    for case in RETRIEVAL_CASES:
        ids = set()
        for gold in case.gold_chunks:
            matches = [cid for cid, item in all_meta.items()
                       if item["metadata"].get("source_url") == gold.source_url
                       and item["metadata"].get("chunk_type") == "child"
                       and item["text"].startswith(f"## {gold.section}")]
            if len(matches) != 1:
                raise ValueError(f"Gold无法唯一定位: {case.case_id} {gold.section}")
            ids.update(matches)
        result[case.case_id] = frozenset(ids)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-embedding", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("tests/quality_reports/git_generations_real.json"))
    args = parser.parse_args()
    with db_module.engine.connect() as connection:
        mysql_version = connection.execute(text("SELECT VERSION()")).scalar()
    db_module.Base.metadata.create_all(db_module.engine)
    snapshot = read_snapshot(BASE_DIR)
    original_settings = {name: getattr(settings, name) for name in (
        "KB_SYNC_MODE", "KB_SOURCE_ID", "KB_VECTOR_DIR", "KB_SYNC_STATE_DIR", "KB_GIT_SUBDIR")}
    source_id = f"eval-{uuid.uuid4().hex}"
    real_embed = embedder.embed_texts
    inputs = []

    def measured(texts):
        inputs.extend(texts)
        if args.real_embedding:
            return real_embed(texts)
        return [[(hashlib.sha256(t.encode()).digest()[i % 32] + 1) / 256
                 for i in range(embedder.EMBED_DIM)] for t in texts]

    report = {"scope": "isolated source, real MySQL and Chroma, temporary vectors; no production pointer changes",
              "mysql_version": mysql_version, "real_embedding": args.real_embedding,
              "source_commit": snapshot.commit, "documents": len(snapshot.docs), "phases": {}}
    try:
        with tempfile.TemporaryDirectory(prefix="git_sync_eval_") as directory:
            settings.KB_SYNC_MODE, settings.KB_SOURCE_ID = "git", source_id
            settings.KB_VECTOR_DIR = str(Path(directory) / "vectors")
            settings.KB_SYNC_STATE_DIR = str(Path(directory) / "state")
            settings.KB_GIT_SUBDIR = "docs/knowledge"
            with patch.object(embedder, "embed_texts", measured), patch.object(sync, "embed_texts", measured):
                baseline = ChromaStore(settings.KB_VECTOR_DIR, "eval_full_baseline")
                start = time.perf_counter()
                for doc in snapshot.docs:
                    meta = {**doc.meta, "source_url": f"docs/knowledge/{doc.rel_path}"}
                    reference = KnowledgeDoc(doc.rel_path, doc.doc_id, meta, doc.content)
                    sync._sync_doc(reference, sync._doc_hash(reference), baseline, sync.SyncReport())
                report["phases"]["full_rebuild_reference"] = {
                    "embedding_inputs": len(inputs), "chunks": baseline.count(),
                    "elapsed_ms": round((time.perf_counter() - start) * 1000, 2)}

                with ThreadPoolExecutor(max_workers=10) as executor:
                    initial_deliveries = list(executor.map(lambda _: g.enqueue(snapshot), range(10)))
                assert len({j["id"] for j in initial_deliveries}) == 1
                report["first_source_concurrent_enqueue"] = {"requests": 10, "jobs": 1}

                def build(value, label):
                    before = len(inputs)
                    job = g.enqueue(value)
                    job = g.process_job(job["id"])
                    assert job["status"] == "prepared", job
                    g.publish(job["id"])
                    report["phases"][label] = {**job["report"], "measured_embedding_inputs": len(inputs) - before}
                    return job

                first = build(snapshot, "initial_generation")
                new_store = create_store()
                before = len(inputs)
                with ThreadPoolExecutor(max_workers=10) as executor:
                    duplicates = list(executor.map(lambda _: g.enqueue(snapshot), range(10)))
                assert {j["id"] for j in duplicates} == {first["id"]}
                report["phases"]["ten_duplicate_deliveries"] = {
                    "jobs": len({j["id"] for j in duplicates}), "embedding_inputs": len(inputs) - before}

                if args.real_embedding:
                    query_vectors = measured([case.query for case in RETRIEVAL_CASES])
                    vector_by_case = dict(zip((c.case_id for c in RETRIEVAL_CASES), query_vectors, strict=True))
                    comparisons = []
                    for label, store in (("full_rebuild_reference", baseline), ("git_generation", new_store)):
                        def search(case):
                            return retrieve(case.query, store=store, top_k=5,
                                            query_vector=vector_by_case[case.case_id]), None
                        comparisons.append(_evaluate(label, search, gold_for(store)))
                    report["retrieval_60"] = comparisons

                changed = dict(snapshot.files)
                target = "vpn/cert-renewal.md"
                changed[target] += "\n## 同步评测新增小节\n\n这是独立评测中新加入的测试知识片段。\n".encode()
                body = read_snapshot_from_files("1" * 40, changed)
                before = len(inputs)
                start = time.perf_counter()
                changed_reference = ChromaStore(settings.KB_VECTOR_DIR, "eval_changed_reference")
                for doc in body.docs:
                    reference = KnowledgeDoc(doc.rel_path, doc.doc_id,
                        {**doc.meta, "source_url": f"docs/knowledge/{doc.rel_path}"}, doc.content)
                    sync._sync_doc(reference, sync._doc_hash(reference), changed_reference, sync.SyncReport())
                report["phases"]["changed_full_rebuild_reference"] = {
                    "embedding_inputs": len(inputs) - before, "chunks": changed_reference.count(),
                    "elapsed_ms": round((time.perf_counter() - start) * 1000, 2)}
                build(body, "one_document_body_change")
                changed[target] = changed[target].replace(b"---\n", b"---\nsync_eval_note: metadata-only\n", 1)
                metadata = read_snapshot_from_files("2" * 40, changed)
                build(metadata, "metadata_only_change")

                deleted_path = "email/config-guide.md"
                deleted_id = next(d.doc_id for d in metadata.docs if d.rel_path == deleted_path)
                broken = dict(changed)
                del broken[deleted_path]
                broken[target] += b"\n## Failure injection\nnew cache miss\n"
                job = g.enqueue(read_snapshot_from_files("3" * 40, broken))
                current_collection = create_store().collection_name
                with patch.object(embedder, "embed_texts", side_effect=RuntimeError("injected provider failure")):
                    failed = g.process_job(job["id"])
                assert failed["status"] == "failed"
                assert create_store().collection_name == current_collection
                assert deleted_id not in {item["metadata"].get("doc_id") for item in create_store().get_all_meta().values()}
                g.rollback()
                assert deleted_id not in {item["metadata"].get("doc_id") for item in create_store().get_all_meta().values()}
                report["fault_injection"] = {"failed_build_kept_active": True,
                    "confirmed_delete_hidden_on_failed_build": True, "rollback_did_not_restore_deleted": True}
                report["retained_generations"] = 3
                report["storage_bytes"] = sum(p.stat().st_size for p in Path(directory).rglob("*") if p.is_file())
    finally:
        with db_module.SessionLocal.begin() as db:
            db.execute(delete(KbSyncJob).where(KbSyncJob.source_id == source_id))
            db.execute(delete(KbSource).where(KbSource.id == source_id))
        for name, value in original_settings.items():
            setattr(settings, name, value)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({"report": str(args.output), "phases": report["phases"],
                      "fault_injection": report.get("fault_injection")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

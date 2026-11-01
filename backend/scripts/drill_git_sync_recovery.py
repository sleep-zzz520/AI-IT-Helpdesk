"""真实进程崩溃及停写备份恢复演练，所有写入均位于临时目录。

PYTHONPATH=backend python backend/scripts/drill_git_sync_recovery.py --output report.json
使用实际 SQLite/Chroma、持久任务和 worker；embedding 是确定性替身。
SIGKILL 仅模拟进程崩溃，不能证明断电时文件系统/磁盘的持久性。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SOURCE_ID = "recovery-drill-isolated"
DOCUMENT_A = "87103426-6789-4a36-8dc5-e38f82e5af9a"
DOCUMENT_B = "19b0e2bb-e111-422a-b589-8340a93ffac3"
BACKEND = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def deterministic_vectors(texts):
    # 非模型质量测试；保留真向量持久化、混合检索及父块回填调用链。
    return [[(hashlib.sha256(text.encode()).digest()[i] + 1) / 256 for i in range(8)]
            for text in texts]


def configure(root):
    # 子进程内覆盖配置；不读取/连接生产库，不运行含生产迁移/种子的 init_db。
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import db
    from app.config import settings
    from app.rag import embedder
    from app.rag import generations as g

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db.engine.dispose()
    db.engine = create_engine(f"sqlite:///{root / 'jobs.sqlite3'}", connect_args={"check_same_thread": False})
    db.SessionLocal = sessionmaker(bind=db.engine, autoflush=False, expire_on_commit=False)
    settings.KB_SYNC_MODE = "git"
    settings.KB_SOURCE_ID = SOURCE_ID
    settings.KB_VECTOR_DIR = str(root / "vectors")
    settings.KB_SYNC_STATE_DIR = str(root / "state")
    settings.KB_GIT_SUBDIR = "docs/knowledge"
    settings.KB_GIT_EXPORT = ""
    settings.KB_PIPELINE_EPOCH = "deterministic-process-recovery-drill-v1"
    settings.RERANK_ENABLED = False
    settings.VECTOR_MIN_SCORE = 0.0
    embedder.EMBED_DIM = 8
    embedder.embed_texts = deterministic_vectors
    g.LEASE_SECONDS = 2
    g.MAX_ATTEMPTS = 3
    db.Base.metadata.create_all(db.engine)
    return db, g


def snapshot(version, *, delete_a=False):
    from app.rag.git_source import read_snapshot_from_files

    files = {}
    for identity, name in ((DOCUMENT_A, "alpha"), (DOCUMENT_B, "beta")):
        if identity == DOCUMENT_A and delete_a:
            continue
        body = (f"---\ndocument_id: {identity}\nscenario: vpn\nstatus: active\n"
                f"valid_to: '2099-12-31'\n---\n# VPN {name}\n\n"
                f"## 证书续期\nVPN certutil {name} {name}fault recovery marker {version} 800\n\n"
                f"## 验证\nVPN {name} 证书续期完成后检查连接与有效期。版本 {version}。\n"
                "## 日志\n保存认证审计事件与连接结果。\n"
                "## 联系方式\n异常时交由运维人工处理。\n")
        files[f"{name}.md"] = body.encode()
    return read_snapshot_from_files(f"{version:040x}", files)


def hold(marker, value):
    write_json(marker, value)
    threading.Event().wait()  # 仅子进程故障注入断点，父进程负责 SIGKILL 和超时。


def inspect_generation(g, *, expect_denied=False):
    from app.rag.retriever import RetrievalStats, retrieve

    state = g.state()
    store = g.resolve_store()
    all_chunks = store.get_all_meta()
    stats = RetrievalStats()
    hits = retrieve("betafault", scenario="vpn", top_k=5,
                    query_vector=deterministic_vectors(["VPN certutil beta"])[0], stats=stats,
                    debug=True)
    assert hits and stats.vector_recall > 0 and stats.bm25_recall > 0, stats
    assert all(hit.parent_text and hit.metadata["chunk_type"] == "child" for hit in hits), "父块回填失败"
    for hit in hits:
        assert store.get_by_ids([hit.metadata["parent_id"]])[hit.metadata["parent_id"]] == hit.parent_text
    visible = sorted({item["metadata"]["doc_id"] for item in all_chunks.values()})
    denied = sorted(g.denied_ids())
    if expect_denied:
        assert DOCUMENT_A in denied and DOCUMENT_A not in visible
        assert all(hit.metadata["doc_id"] != DOCUMENT_A for hit in hits)
        assert all(hit.metadata["doc_id"] != DOCUMENT_A for hit in stats.debug_vector_hits)
        assert all(hit.metadata["doc_id"] != DOCUMENT_A for hit in stats.debug_bm25_hits)
    else:
        assert visible == sorted([DOCUMENT_A, DOCUMENT_B])
    canonical = json.dumps(all_chunks, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"active_generation": state["active_generation"], "source_commit": state["source_commit"],
            "active_collection": state["active_collection"], "visible_documents": visible,
            "visible_chunks": len(all_chunks), "denied_ids": denied,
            "content_metadata_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "parent_sha256": sorted(hashlib.sha256(hit.parent_text.encode()).hexdigest() for hit in hits),
            "hybrid": {"vector_recall": stats.vector_recall, "bm25_recall": stats.bm25_recall,
                       "hits": len(hits), "all_parent_contexts_verified": True}}


def child(args):
    db, g = configure(args.root)
    if args.phase == "baseline":
        job = g.enqueue(snapshot(1))
        job = g.process_job(job["id"])
        assert job["status"] == "prepared", job
        g.publish(job["id"])
        write_json(args.marker, {"job": job, "inspection": inspect_generation(g)})
    elif args.phase == "queued":
        job = g.enqueue(snapshot(2))
        assert job["status"] == "queued"
        hold(args.marker, {"job": job})
    elif args.phase in {"partial", "recover"}:
        from app.config import settings
        from app.models import KbSource, KbSyncJob
        from app.rag.store import ChromaStore

        with db.SessionLocal() as session:
            source = session.get(KbSource, SOURCE_ID)
            job_id = session.query(KbSyncJob).filter_by(source_id=SOURCE_ID, seq=source.desired_seq).one().id
        if args.phase == "partial":
            original = ChromaStore.upsert_chunks

            def interrupted_upsert(store, chunks, vectors):
                original(store, chunks[:1], vectors[:1])
                persisted = store.get_chunks([chunks[0].id])
                assert chunks[0].id in persisted
                hold(args.marker, {"job": g.get_job(job_id), "partial_chunks": len(persisted),
                                   "candidate_ready": store.is_build_ready(),
                                   "active_generation": g.state()["active_generation"],
                                   "vector_dir": settings.KB_VECTOR_DIR})

            ChromaStore.upsert_chunks = interrupted_upsert
            g.run_worker(threading.Event())
        else:
            stop = threading.Event()
            worker = threading.Thread(target=g.run_worker, args=(stop,), daemon=True)
            started = time.perf_counter()
            worker.start()
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                job = g.get_job(job_id)
                if job["status"] == "prepared":
                    stop.set()
                    worker.join(5)
                    assert not worker.is_alive()
                    hold(args.marker, {"job": job, "recovery_ms": round((time.perf_counter() - started) * 1000, 2)})
                assert job["status"] in {"queued", "running"}, job
                time.sleep(0.1)
            raise TimeoutError("worker 未在时限内恢复任务")
    elif args.phase == "published":
        state = g.state()
        job = g.publish(state["latest_job"]["id"])
        hold(args.marker, {"job": job, "inspection": inspect_generation(g)})
    elif args.phase == "inspect":
        write_json(args.marker, inspect_generation(g, expect_denied=args.expect_denied))
    elif args.phase == "deny":
        queued = g.enqueue(snapshot(3, delete_a=True))
        assert queued["status"] == "queued"
        write_json(args.marker, inspect_generation(g, expect_denied=True))
    elif args.phase == "rollback":
        before = g.state()["active_generation"]
        g.rollback()
        value = inspect_generation(g, expect_denied=True)
        assert value["active_generation"] != before
        write_json(args.marker, value)
    db.engine.dispose()


def run_child(root, phase, *, kill=False, expect_denied=False):
    marker = Path(root).parent / f"{Path(root).name}-{phase}.json"
    marker.unlink(missing_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), "--child", "--root", str(root),
               "--phase", phase, "--marker", str(marker)]
    if expect_denied:
        command.append("--expect-denied")
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "ZHIPU_API_KEY": "recovery-drill-no-network",
           "RERANK_ENABLED": "false", "ANONYMIZED_TELEMETRY": "false"}
    log_path = marker.with_suffix(".log")
    started = time.perf_counter()
    with log_path.open("w+") as log:
        process = subprocess.Popen(command, cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 50
            while not marker.exists():
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(f"阶段 {phase} 退出码 {process.returncode}: {log.read()[-5000:]}")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"阶段 {phase} 未到达持久化断点")
                time.sleep(0.05)
            value = json.loads(marker.read_text())
            if kill:
                process.send_signal(signal.SIGKILL)
                assert process.wait(timeout=10) == -signal.SIGKILL
                value["termination_signal"] = "SIGKILL"
            else:
                assert process.wait(timeout=15) == 0, f"阶段 {phase} 未正常退出"
            value["process_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            return value
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def tree_digest(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(root).rglob("*")) if path.is_file()}


def backup_restore(source, backup, restored):
    """仅接受本工具已停写的临时目录；不用于在线备份或生产数据库恢复。"""
    started = time.perf_counter()
    backup.mkdir()
    with sqlite3.connect(source / "jobs.sqlite3") as live, sqlite3.connect(backup / "jobs.sqlite3") as destination:
        live.backup(destination)
    for name in ("vectors", "state"):
        shutil.copytree(source / name, backup / name)
    backup_hashes = tree_digest(backup)
    shutil.copytree(backup, restored)
    assert tree_digest(restored) == backup_hashes
    # 持久任务使用绝对快照路径，恢复到新根后要重定位；字节/摘要不变。
    with sqlite3.connect(restored / "jobs.sqlite3") as connection:
        rows = connection.execute("SELECT id, snapshot_path FROM kb_sync_jobs").fetchall()
        for job_id, old_path in rows:
            relative = Path(old_path).relative_to(source / "state")
            new_path = restored / "state" / relative
            assert new_path.is_file()
            connection.execute("UPDATE kb_sync_jobs SET snapshot_path=? WHERE id=?", (str(new_path), job_id))
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    return {"method": "stopped writers; SQLite backup API + directory copy; restored snapshot paths relocated",
            "backup_files": len(backup_hashes), "backup_bytes": sum(p.stat().st_size for p in backup.rglob("*") if p.is_file()),
            "copied_file_checksums_equal": True, "snapshot_paths_relocated": len(rows),
            "sqlite_integrity_check": "ok", "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}


def run_drill():
    started = time.perf_counter()
    report = {"scope": "isolated temporary SQLite + real persistent Chroma + real worker subprocesses",
              "embedding": "deterministic 8-dimensional replacement; no provider/model quality evaluation",
              "crash_model": "SIGKILL process crash; does not prove power-loss or filesystem fsync durability",
              "lease_seconds_override": 2, "production_data_touched": False, "phases": {}}
    with tempfile.TemporaryDirectory(prefix="git-sync-recovery-") as temporary:
        root = Path(temporary) / "live"
        phases = report["phases"]
        phases["baseline"] = run_child(root, "baseline")
        initial_id = phases["baseline"]["job"]["id"]
        phases["queued_kill"] = run_child(root, "queued", kill=True)
        job_id = phases["queued_kill"]["job"]["id"]
        phases["partial_write_kill"] = run_child(root, "partial", kill=True)
        assert phases["partial_write_kill"]["active_generation"] == initial_id
        assert phases["partial_write_kill"]["candidate_ready"] is False
        assert phases["partial_write_kill"]["job"]["id"] == job_id
        phases["recovered_prepared_kill"] = run_child(root, "recover", kill=True)
        assert phases["recovered_prepared_kill"]["job"]["attempts"] == 2
        phases["published_kill"] = run_child(root, "published", kill=True)
        phases["restart_after_publish"] = run_child(root, "inspect")
        assert phases["restart_after_publish"]["active_generation"] == job_id
        assert phases["published_kill"]["inspection"]["content_metadata_sha256"] == phases["restart_after_publish"]["content_metadata_sha256"]
        phases["deny_before_backup"] = run_child(root, "deny")
        backup = Path(temporary) / "backup"
        restored = Path(temporary) / "restored"
        report["backup_restore"] = backup_restore(root, backup, restored)
        phases["restored_inspection"] = run_child(restored, "inspect", expect_denied=True)
        assert {
            k: v for k, v in phases["restored_inspection"].items() if k != "process_elapsed_ms"
        } == {k: v for k, v in phases["deny_before_backup"].items() if k != "process_elapsed_ms"}
        phases["restored_rollback_deny"] = run_child(restored, "rollback")
        assert phases["restored_rollback_deny"]["active_generation"] == initial_id
        report["backup_restore"].update(generation_documents_and_parent_contexts_equal=True,
                                       deny_did_not_resurrect_on_rollback=True)
    killed = sum(phase.get("termination_signal") == "SIGKILL" for phase in phases.values())
    report.update(passed=True, actual_sigkill_count=killed,
                  elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                  recovery_ms=phases["recovered_prepared_kill"]["recovery_ms"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tests/quality_reports/git_sync_recovery.json"))
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--marker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--phase", help=argparse.SUPPRESS)
    parser.add_argument("--expect-denied", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        child(args)
        return
    try:
        report = run_drill()
    except Exception as exc:
        write_json(args.output, {"passed": False, "error": f"{type(exc).__name__}: {exc}"})
        raise
    write_json(args.output, report)
    print(json.dumps({"passed": True, "report": str(args.output), "actual_sigkill_count": report["actual_sigkill_count"],
                      "recovery_ms": report["recovery_ms"], "backup_restore": report["backup_restore"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

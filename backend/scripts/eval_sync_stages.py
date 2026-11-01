"""在隔离source/临时Chroma目录实测解析、切分与嵌入调用；MySQL为真实实例。"""
import argparse
import hashlib
import json
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete, text

from app import db
from app.config import BASE_DIR, settings
from app.models import KbSource, KbSyncJob
from app.rag import embedder, git_source
from app.rag import generations as g


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-embedding", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("tests/quality_reports/sync_stages_real.json"))
    args = parser.parse_args()
    db.Base.metadata.create_all(db.engine)
    db._ensure_columns()
    with db.engine.connect() as connection:
        version = connection.execute(text("SELECT VERSION()")).scalar()
    original = git_source.read_snapshot(BASE_DIR)
    real_parse, real_split, real_embed = git_source._parse_content, g.split_markdown, embedder.embed_texts
    calls = {"parsed_documents": 0, "chunked_documents": 0, "embedding_inputs": 0}
    report = {"scope": "isolated MySQL source and temporary Chroma; raw files from fixed Git, variation commits are synthetic",
              "source_commit": original.commit, "mysql_version": version, "documents": len(original.docs),
              "real_embedding": args.real_embedding, "phases": {}}
    source_id = f"stages-eval-{uuid.uuid4().hex}"

    def parse(*a, **kw):
        calls["parsed_documents"] += 1
        return real_parse(*a, **kw)

    def split(*a, **kw):
        calls["chunked_documents"] += 1
        return real_split(*a, **kw)

    def embed(texts):
        calls["embedding_inputs"] += len(texts)
        if args.real_embedding:
            return real_embed(texts)
        return [[(hashlib.sha256(t.encode()).digest()[i % 32] + 1) / 256
                 for i in range(embedder.EMBED_DIM)] for t in texts]

    names = ("KB_SYNC_MODE", "KB_SOURCE_ID", "KB_VECTOR_DIR", "KB_SYNC_STATE_DIR", "KB_GIT_EXPORT")
    prior = {name: getattr(settings, name) for name in names}
    try:
        with tempfile.TemporaryDirectory(prefix="sync_stages_eval_") as directory:
            root = Path(directory)
            settings.KB_SYNC_MODE, settings.KB_SOURCE_ID = "git", source_id
            settings.KB_VECTOR_DIR, settings.KB_SYNC_STATE_DIR = str(root / "vectors"), str(root / "state")
            settings.KB_GIT_EXPORT = str(root / "input.json")

            def phase(label, snapshot):
                # 评测输入准备不属于应用调用；对enqueue→构建→publish全过程计数。
                git_source.write_export(snapshot, Path(settings.KB_GIT_EXPORT))
                for key in calls:
                    calls[key] = 0
                start = time.perf_counter()
                with patch.object(git_source, "_parse_content", parse), patch.object(g, "split_markdown", split), patch.object(embedder, "embed_texts", embed):
                    job = g.enqueue()
                    job = g.process_job(job["id"])
                    assert job["status"] == "prepared", job
                    g.publish(job["id"])
                report["phases"][label] = {"actual_calls": dict(calls), "worker": job["report"],
                    "full_flow_elapsed_ms": round((time.perf_counter() - start) * 1000, 2)}
                return dict(calls)

            first = phase("cold", original)
            assert first["parsed_documents"] == first["chunked_documents"] == len(original.docs)
            warm = phase("new_commit_same_bytes", git_source.read_snapshot_from_files("1" * 40, original.files))
            assert warm == {"parsed_documents": 0, "chunked_documents": 0, "embedding_inputs": 0}
            changed = dict(original.files)
            changed["vpn/cert-renewal.md"] += "\n## 阶段缓存验收\n\n这是独立验收中新增的唯一知识片段。\n".encode()
            one = phase("one_document_change", git_source.read_snapshot_from_files("2" * 40, changed))
            assert one == {"parsed_documents": 1, "chunked_documents": 1, "embedding_inputs": 1}
            changed["vpn/cert-renewal.md"] = changed["vpn/cert-renewal.md"].replace(b"---\n", b"---\nstage_eval_note: metadata-only\n", 1)
            metadata = phase("metadata_only", git_source.read_snapshot_from_files("3" * 40, changed))
            assert metadata == {"parsed_documents": 1, "chunked_documents": 1, "embedding_inputs": 0}
            upgraded = git_source.read_snapshot_from_files("4" * 40, changed)
            real_profile = git_source.parser_profile
            with patch.object(git_source, "parser_profile", lambda ids: real_profile(ids) + "-eval-parser-upgrade"):
                value = phase("parser_upgrade_same_output", upgraded)
                assert value == {"parsed_documents": len(original.docs), "chunked_documents": 0, "embedding_inputs": 0}
            report["storage_bytes"] = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    finally:
        with db.SessionLocal.begin() as session:
            session.execute(delete(KbSyncJob).where(KbSyncJob.source_id == source_id))
            session.execute(delete(KbSource).where(KbSource.id == source_id))
        for name, value in prior.items():
            setattr(settings, name, value)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"report": str(args.output), "phases": {k: {"calls": v["actual_calls"],
        "elapsed_ms": v["full_flow_elapsed_ms"]} for k, v in report["phases"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()

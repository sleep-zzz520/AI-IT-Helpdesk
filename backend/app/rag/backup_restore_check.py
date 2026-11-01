"""在独立 MySQL schema 和临时向量目录中校验停写知识备份。

仅验证持久产物恢复与检索链路，不调用新 embedding，不评估模型质量。
本模块不能对生产 schema 执行迁移或重定位。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_FILES = 100_000
MAX_EXTRACTED_BYTES = 8 * 1024**3
FREE_SPACE_RESERVE = 64 * 1024**2
BASELINE_FIELDS = ("active_generation", "source_commit", "active_collection", "doc_count",
                   "content_metadata_sha256", "denied_ids")


def require_isolated_database(name):
    if not isinstance(name, str) or not re.fullmatch(r"helpdesk_restore_[a-f0-9]+", name) or len(name) > 64:
        raise ValueError("恢复校验仅允许 helpdesk_restore_<hex> 隔离数据库")
    return name


def canonical_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_knowledge_dump(path):
    """受控mysqldump只允许两张知识表，导入前拒绝跨schema语句。"""
    value = Path(path).read_text(encoding="utf-8")
    # 先移除SQL字符串，避免知识元数据里的SQL字样被误当作操作。
    value = re.sub(r"'(?:\\.|''|[^'\\])*'", "''", value, flags=re.S)
    value = re.sub(r"--[^\n]*|\#[^\n]*", "", value)
    value = re.sub(r"/\*(?!\!)[\s\S]*?\*/", "", value)
    value = re.sub(r"/\*!\d*\s*([\s\S]*?)\*/", r"\1", value)
    if re.search(r"`[^`]+`\s*\.", value) or re.search(r"\b(?:DATABASE|SCHEMA|USE|GLOBAL)\b", value, re.I):
        raise ValueError("知识dump不得选择或操作数据库/全局设置")
    table = r"`(?:kb_sources|kb_sync_jobs)`"
    session_setting = r"SET\s+(?:@|NAMES\b|(?:TIME_ZONE|UNIQUE_CHECKS|FOREIGN_KEY_CHECKS|SQL_MODE|SQL_NOTES|CHARACTER_SET_CLIENT|CHARACTER_SET_RESULTS|COLLATION_CONNECTION|SQL_LOG_BIN)\b)"
    allowed = rf"(?:{session_setting}|UNLOCK\s+TABLES\s*$|(?:DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?|CREATE\s+TABLE\s+|LOCK\s+TABLES\s+|ALTER\s+TABLE\s+|INSERT\s+INTO\s+){table}(?:\s|\(|$))"
    statements = [part.strip() for part in value.split(";") if part.strip()]
    if not statements or any(not re.match(allowed, statement, re.I) for statement in statements):
        raise ValueError("知识dump包含两张知识表之外的语句")


def safe_extract_archive(archive_path, destination):
    """先校验整张文件清单，手动复制普通文件，不应用归档权限或链接。"""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    total = 0
    members = []
    seen = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            parts = name.split("/")
            if (not name or "\\" in name or any(ord(char) < 32 for char in name)
                    or any(part in {"", ".", ".."} for part in parts)
                    or parts[0] != "rag" or PurePosixPath(name).is_absolute()
                    or not (member.isfile() or member.isdir()) or member.size < 0
                    or name in seen):
                raise ValueError("归档包含不安全路径、重复项、链接或特殊文件")
            if member.isdir() and member.size:
                raise ValueError("归档目录包含异常数据")
            if not (destination / name).resolve().is_relative_to(destination):
                raise ValueError("归档路径逃逸")
            seen.add(name)
            total += member.size
            members.append((member, name))
            if len(members) > MAX_ARCHIVE_FILES or total > MAX_EXTRACTED_BYTES:
                raise ValueError("归档超过恢复演练大小限制")
        if not members or shutil.disk_usage(destination).free < total + FREE_SPACE_RESERVE:
            raise ValueError("恢复目录磁盘空间不足或归档为空")
        for member, name in members:
            target = destination / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            reader = archive.extractfile(member)
            if reader is None:
                raise ValueError("归档普通文件无法读取")
            with reader, target.open("xb") as output:
                shutil.copyfileobj(reader, output)
            if target.stat().st_size != member.size:
                raise ValueError("归档文件大小不符")
    for name in ("kb_chroma", "sync"):
        if not (destination / "rag" / name).is_dir():
            raise ValueError("归档缺少知识向量库或同步状态目录")
    return {"files": sum(member.isfile() for member, _ in members), "uncompressed_bytes": total}


def relocate_snapshot_path(value, restored_sync):
    prefix = "/.rag/sync/"
    if not isinstance(value, str) or not value.startswith(prefix) or "\\" in value:
        raise ValueError("快照路径必须来自原 /.rag/sync 目录")
    relative = value[len(prefix):]
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError("快照路径包含逃逸或异常路径段")
    root = Path(restored_sync).resolve()
    target = root / relative
    if not target.resolve().is_relative_to(root) or not target.is_file() or target.is_symlink():
        raise ValueError("恢复快照不存在或指向隔离目录外")
    return str(target)


def prepare_clone_database(db_module, database_name, restored_sync):
    """仅补克隆库kb_sync_jobs新增字段，然后迁移其快照路径。"""
    from sqlalchemy import inspect, text

    require_isolated_database(database_name)
    if db_module.engine.url.database != database_name:
        raise ValueError("实际连接数据库与隔离schema不一致")
    columns = {column["name"] for column in inspect(db_module.engine).get_columns("kb_sync_jobs")}
    # 常量DDL来自应用既有迁移表；禁止调用含seed及其他业务表迁移的init_db。
    additions = db_module._EXTRA_COLUMNS.get("kb_sync_jobs", [])
    with db_module.engine.begin() as connection:
        for column, ddl in additions:
            if not re.fullmatch(r"[a-z_]+", column):
                raise ValueError("非法迁移列名")
            if column not in columns:
                connection.execute(text(f"ALTER TABLE kb_sync_jobs ADD COLUMN {column} {ddl}"))
        rows = connection.execute(text("SELECT id, snapshot_path FROM kb_sync_jobs")).fetchall()
        relocated = [(relocate_snapshot_path(old_path, restored_sync), job_id) for job_id, old_path in rows]
        for path, job_id in relocated:
            connection.execute(text("UPDATE kb_sync_jobs SET snapshot_path=:path WHERE id=:job_id"),
                               {"path": path, "job_id": job_id})
    return {"snapshot_paths_relocated": len(relocated),
            "compatibility_columns_added": [column for column, _ in additions if column not in columns]}


def compare_baseline(expected, actual):
    if not isinstance(expected, dict) or any(field not in expected for field in BASELINE_FIELDS):
        raise ValueError("备份manifest缺少完整baseline")
    for field in BASELINE_FIELDS:
        if expected[field] != actual[field]:
            raise ValueError(f"恢复结果与备份baseline不一致: {field}")


def verify_restored_generation(g, expected):
    from app import db
    from app.config import settings
    from app.models import KbSyncJob
    from app.rag import embedder
    from app.rag.retriever import RetrievalStats, retrieve

    settings.RERANK_ENABLED = False
    def forbid_new_embeddings(*_args, **_kwargs):
        raise AssertionError("恢复演练禁止发起新的嵌入请求")
    embedder.embed_texts = forbid_new_embeddings
    state = g.state()
    if not state["active_generation"]:
        raise ValueError("备份没有可验证的在岗generation")
    with db.SessionLocal() as session:
        job = session.get(KbSyncJob, state["active_generation"])
        g._verify_generation(job)
    store = g.resolve_store()
    chunks = store.get_all_meta()
    actual = {field: state[field] for field in BASELINE_FIELDS[:4]}
    actual.update(content_metadata_sha256=canonical_digest(chunks), denied_ids=sorted(g.denied_ids()))
    compare_baseline(expected, actual)
    candidates = [cid for cid, item in chunks.items()
                  if item["metadata"].get("chunk_type") == "child"
                  and item["metadata"].get("scenario") == "vpn"]
    if not candidates:
        raise ValueError("恢复库缺少VPN子块，无法验证真实检索路径")
    vectors = store.get_vectors(candidates[:1])
    query_vector = vectors.get(candidates[0])
    if not query_vector or not g._valid_vector(query_vector):
        raise ValueError("恢复库VPN子块向量不完整")
    stats = RetrievalStats()
    hits = retrieve("VPN 证书过期 Error 800 续期", scenario="vpn", top_k=5,
                    query_vector=query_vector, stats=stats, debug=True, joint=False)
    if not hits or stats.vector_recall <= 0 or stats.bm25_recall <= 0:
        raise ValueError("恢复后的混合检索没有双路命中")
    for hit in hits:
        parent_id = hit.metadata.get("parent_id")
        parent = store.get_by_ids([parent_id]).get(parent_id) if parent_id else None
        if (hit.metadata.get("chunk_type") != "child" or not parent
                or not hit.parent_text or parent != hit.parent_text):
            raise ValueError("恢复后的父块上下文回填不完整")
    forbidden = set(actual["denied_ids"])
    for hit in [*hits, *(stats.debug_vector_hits or []), *(stats.debug_bm25_hits or [])]:
        if hit.metadata.get("doc_id") in forbidden:
            raise ValueError("恢复后的检索使已删除文档重新出现")
    return {"baseline": actual, "hybrid": {"vector_recall": stats.vector_recall,
            "bm25_recall": stats.bm25_recall, "hits": len(hits), "all_parent_contexts_verified": True},
            "denied_documents_not_resurrected": True}


def run_check(args):
    started = time.perf_counter()
    name = require_isolated_database(os.environ.get("MYSQL_DB", ""))
    from app.config import settings

    if name != settings.MYSQL_DB:
        raise ValueError("配置数据库不等于显式隔离数据库")
    manifest = json.loads(args.manifest.read_text())
    expected = manifest.get("baseline")
    if not isinstance(expected, dict) or not expected.get("active_generation"):
        raise ValueError("manifest没有可验证的在岗generation baseline")
    drills = Path("/.rag/drills")
    drills.mkdir(parents=True, exist_ok=True)
    if drills != drills.resolve() or drills.is_symlink():
        raise ValueError("演练根目录不能是符号链接")
    with tempfile.TemporaryDirectory(prefix="restore-", dir=drills) as directory:
        root = Path(directory)
        archive = safe_extract_archive(args.archive, root)
        settings.KB_SYNC_MODE = "git"
        settings.KB_VECTOR_DIR = str(root / "rag" / "kb_chroma")
        settings.KB_SYNC_STATE_DIR = str(root / "rag" / "sync")
        settings.KB_GIT_EXPORT = ""
        settings.KB_ACTIVE_FILE = root / "rag" / "kb_active.txt"
        from app import db
        from app.rag import generations as g

        try:
            migrated = prepare_clone_database(db, name, settings.KB_SYNC_STATE_DIR)
            validation = verify_restored_generation(g, expected)
        finally:
            # _verify_generation可能先于resolve_store失败；也须关闭此前创建的临时Chroma。
            from chromadb.api.shared_system_client import SharedSystemClient

            for system in tuple(SharedSystemClient._identifier_to_system.values()):
                system.stop()
            SharedSystemClient.clear_system_cache()
            db.engine.dispose()
    return {"passed": True, "scope": "restore validation in isolated MySQL schema and temporary Chroma directory",
            "model_quality_evaluation": False, "new_embedding_requests": 0, "database": name,
            "archive": archive, **migrated, **validation,
            "temporary_files_removed": True, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run_check(args)
    except Exception as exc:
        # 不输出连接串、SQL参数、知识正文或密钥。
        print(json.dumps({"passed": False, "error_type": type(exc).__name__}, ensure_ascii=False))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

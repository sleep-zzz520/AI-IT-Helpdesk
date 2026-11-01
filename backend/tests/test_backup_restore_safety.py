"""恢复工具的隔离护栏与产物校验；不连接MySQL或外部模型。"""
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text

from app.rag.backup_restore_check import (
    BASELINE_FIELDS,
    canonical_digest,
    compare_baseline,
    prepare_clone_database,
    relocate_snapshot_path,
    require_isolated_database,
    safe_extract_archive,
    validate_knowledge_dump,
)


@pytest.fixture(autouse=True)
def mock_llm():
    yield


@pytest.mark.parametrize("name", ["it_helpdesk", "helpdesk_restore_", "helpdesk_restore_prod", "helpdesk_restore_ab;DROP", "helpdesk_restore_ABC", "helpdesk_restore_../ab"])
def test_production_or_malformed_database_is_rejected(name):
    with pytest.raises(ValueError):
        require_isolated_database(name)


def archive(path, entries):
    with tarfile.open(path, "w:gz") as target:
        for name, kind in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            if kind == tarfile.REGTYPE:
                item.size = 3
                target.addfile(item, io.BytesIO(b"abc"))
            else:
                item.linkname = "../../outside"
                target.addfile(item)


@pytest.mark.parametrize("name,kind", [
    ("/rag/escape", tarfile.REGTYPE), ("rag/../escape", tarfile.REGTYPE),
    ("rag/x/../../escape", tarfile.REGTYPE), ("rag/x\\escape", tarfile.REGTYPE),
    ("other/file", tarfile.REGTYPE), ("rag/link", tarfile.SYMTYPE),
    ("rag/hardlink", tarfile.LNKTYPE), ("rag/fifo", tarfile.FIFOTYPE),
])
def test_archive_escape_links_and_special_files_rejected(tmp_path, name, kind):
    source = tmp_path / "unsafe.tar.gz"
    archive(source, [(name, kind)])
    with pytest.raises(ValueError):
        safe_extract_archive(source, tmp_path / "restored")
    assert not list((tmp_path / "restored").rglob("*"))


def test_archive_extraction_and_disk_space_check(tmp_path, monkeypatch):
    source = tmp_path / "safe.tar.gz"
    entries = [("rag", tarfile.DIRTYPE), ("rag/kb_chroma", tarfile.DIRTYPE),
               ("rag/kb_chroma/db", tarfile.REGTYPE), ("rag/sync", tarfile.DIRTYPE),
               ("rag/sync/snapshots/snapshot.json", tarfile.REGTYPE)]
    archive(source, entries)
    result = safe_extract_archive(source, tmp_path / "safe")
    assert result == {"files": 2, "uncompressed_bytes": 6}
    assert (tmp_path / "safe/rag/kb_chroma/db").read_bytes() == b"abc"
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(ValueError, match="空间"):
        safe_extract_archive(source, tmp_path / "no-space")
    assert not list((tmp_path / "no-space").rglob("*"))


def test_clone_path_relocation_and_legacy_columns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "restored/sync"
    (root / "snapshots").mkdir(parents=True)
    (root / "snapshots/abc.json").write_text("{}")
    engine = create_engine("sqlite:///helpdesk_restore_abc123")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE kb_sync_jobs (id TEXT PRIMARY KEY, snapshot_path TEXT)"))
        connection.execute(text("INSERT INTO kb_sync_jobs VALUES ('job1','/.rag/sync/snapshots/abc.json')"))
    module = SimpleNamespace(engine=engine, _EXTRA_COLUMNS={"kb_sync_jobs": [
        ("error_kind", "VARCHAR(32) NULL"), ("next_attempt_at", "DATETIME NULL"),
        ("failures_json", "LONGTEXT NULL")]})
    result = prepare_clone_database(module, "helpdesk_restore_abc123", root)
    assert result["snapshot_paths_relocated"] == 1
    assert len(result["compatibility_columns_added"]) == 3
    assert {"error_kind", "next_attempt_at", "failures_json"} <= {c["name"] for c in inspect(engine).get_columns("kb_sync_jobs")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT snapshot_path FROM kb_sync_jobs")).scalar() == str(root / "snapshots/abc.json")
    with pytest.raises(ValueError):
        prepare_clone_database(module, "it_helpdesk", root)
    with pytest.raises(ValueError):
        prepare_clone_database(module, "helpdesk_restore_999", root)
    engine.dispose()


@pytest.mark.parametrize("path", ["/.rag/kb_chroma/a", "/.rag/sync/../a", "/.rag/sync//a", "/tmp/a", "/.rag/sync/missing"])
def test_snapshot_path_rejects_other_roots_escape_and_missing(tmp_path, path):
    with pytest.raises(ValueError):
        relocate_snapshot_path(path, tmp_path)


def test_digest_and_baseline_are_order_independent_but_exact():
    value = {"子块": {"text": "证书续期", "metadata": {"x": 1, "y": 2}}}
    equivalent = {"子块": {"metadata": {"y": 2, "x": 1}, "text": "证书续期"}}
    assert canonical_digest(value) == canonical_digest(equivalent)
    baseline = dict.fromkeys(BASELINE_FIELDS, "same")
    compare_baseline(baseline, dict(baseline))
    for field in BASELINE_FIELDS:
        with pytest.raises(ValueError, match=field):
            compare_baseline(baseline, {**baseline, field: "changed"})


def test_real_mysqldump_session_restore_statements_are_allowed(tmp_path):
    path = tmp_path / "knowledge.sql"
    path.write_text("""/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
CREATE TABLE `kb_sources` (`id` varchar(64) NOT NULL);
""")
    validate_knowledge_dump(path)


@pytest.mark.parametrize("extra", ["USE it_helpdesk;", "/*!40000 CREATE DATABASE production */;", "DROP TABLE `users`;", "INSERT INTO `prod`.`kb_sources` VALUES (1);", "SET GLOBAL max_connections=1;", "SET PASSWORD = 'changed';"])
def test_dump_preflight_rejects_cross_database_or_other_tables(tmp_path, extra):
    source = tmp_path / "dump.sql"
    source.write_text("CREATE TABLE `kb_sources` (id int);" + extra)
    with pytest.raises(ValueError):
        validate_knowledge_dump(source)


def test_mysqldump_knowledge_format_is_accepted(tmp_path):
    source = tmp_path / "dump.sql"
    source.write_text("""-- MySQL dump\n/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
DROP TABLE IF EXISTS `kb_sources`;
CREATE TABLE `kb_sources` (id varchar(64));
LOCK TABLES `kb_sources` WRITE;
/*!40000 ALTER TABLE `kb_sources` DISABLE KEYS */;
INSERT INTO `kb_sources` VALUES ('metadata says USE production; \\'quoted\\'');
/*!40000 ALTER TABLE `kb_sources` ENABLE KEYS */;
UNLOCK TABLES;
""")
    validate_knowledge_dump(source)


def test_shell_uses_explicit_stdin_and_cleans_schema_on_validation_failure(tmp_path):
    project = Path(__file__).resolve().parents[2]
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts/verify_kb_backup.sh"
    shutil.copyfile(project / "scripts/verify_kb_backup.sh", script)
    module_dir = tmp_path / "backend/app/rag"
    module_dir.mkdir(parents=True)
    shutil.copyfile(project / "backend/app/rag/backup_restore_check.py", module_dir / "backup_restore_check.py")
    backups = tmp_path / "backups"
    backups.mkdir()
    prefix = "pre-sync-20261101T123456Z"
    (backups / "latest-kb-backup").write_text(prefix + "\n")
    for suffix in ("rag.tar.gz", "manifest.json"):
        (backups / f"{prefix}-{suffix}").write_text("fixture")
    sql = "CREATE TABLE `kb_sources` (id int);"
    (backups / f"{prefix}-knowledge.sql").write_text(sql)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
stdin=sys.stdin.read()
with Path(os.environ['CALLS']).open('a') as output:
 output.write(json.dumps({'args':args,'stdin':stdin})+'\\n')
if args[:2] == ['compose','run']:
 print('{"passed": false, "error_type": "InjectedFailure"}')
 sys.exit(1)
""")
    docker.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    result = subprocess.run(["bash", str(script)], input="SSH_SCRIPT_MUST_NOT_BE_CONSUMED", text=True,
                            capture_output=True, env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls)})
    assert result.returncode == 1
    operations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(operations) == 4
    assert [entry["stdin"] for entry in operations] == ["", sql, "", ""]
    assert operations[0]["args"][-2] == "create"
    assert operations[-1]["args"][-2] == "cleanup"
    assert operations[0]["args"][-1] == operations[-1]["args"][-1]
    assert operations[-1]["args"][-1].startswith("helpdesk_restore_")

"""独立子进程演练，防止 pytest 全局mock/内存SQL fixture掩盖持久恢复问题。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def mock_llm():
    """本用例不导入应用，所有应用行为均发生于独立子进程。"""
    yield


def test_sigkill_worker_and_stopped_backup_restore(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    output = tmp_path / "recovery-report.json"
    result = subprocess.run(
        [sys.executable, str(backend / "scripts/drill_git_sync_recovery.py"), "--output", str(output)],
        cwd=backend,
        env={**os.environ, "PYTHONPATH": str(backend)},
        capture_output=True,
        text=True,
        timeout=150,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["passed"] is True
    assert report["actual_sigkill_count"] == 4
    phases = report["phases"]
    assert phases["queued_kill"]["job"]["status"] == "queued"
    assert phases["partial_write_kill"]["partial_chunks"] == 1
    assert phases["partial_write_kill"]["candidate_ready"] is False
    assert phases["recovered_prepared_kill"]["job"]["attempts"] == 2
    assert phases["restart_after_publish"]["hybrid"]["all_parent_contexts_verified"] is True
    assert phases["restored_inspection"]["active_generation"] == phases["restart_after_publish"]["active_generation"]
    assert report["backup_restore"]["copied_file_checksums_equal"] is True
    assert report["backup_restore"]["generation_documents_and_parent_contexts_equal"] is True
    assert report["backup_restore"]["deny_did_not_resurrect_on_rollback"] is True

"""Real Git object snapshots: no model, database, or working-tree content reads."""
import hashlib
import json
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from app.rag import git_source as source

ID = "d498e1ba-ac4f-4bce-9664-4b692f156bb6"
ID2 = "74f840c0-8d63-4cdc-b2cf-c06659907cf1"
SHA = "a" * 40


# This module exercises source I/O alone, independent of application LLM fixtures.
@pytest.fixture(autouse=True)
def mock_llm():
    yield


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


def document(identity=ID, body="# Title\ncontent", extra=""):
    return f"---\nscenario: vpn\ndocument_id: {identity}\n{extra}---\n{body}\n".encode()


def commit_file(repo, name="a.md", raw=None):
    path = repo / "docs/knowledge" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(document() if raw is None else raw)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "snapshot")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Snapshot tests")
    git(tmp_path, "config", "user.email", "snapshot@example.invalid")
    return tmp_path


def test_pinned_head_and_dirty_worktree(repo, monkeypatch):
    first = commit_file(repo)
    original = source._git
    calls = []

    def advance(repo_arg, *args):
        result = original(repo_arg, *args)
        calls.append(args)
        if args[0] == "rev-parse":
            commit_file(repo, raw=document(body="changed"))
            (repo / "docs/knowledge/a.md").write_bytes(b"dirty and invalid")
        return result

    monkeypatch.setattr(source, "_git", advance)
    snap = source.read_snapshot(repo)
    assert snap.commit == first
    assert snap.files == {"a.md": document()}
    assert snap.docs[0].doc_id == ID
    assert snap.docs[0].meta["source_url"] == "a.md"
    assert snap.raw_hashes[ID] == hashlib.sha256(document()).hexdigest()
    assert sum(c[0] == "ls-tree" for c in calls) == 1
    assert calls[1][3] == first
    assert snap.snapshot_id == snap.snapshot_hash


def test_rename_retains_identity(repo):
    commit_file(repo)
    before = source.read_snapshot(repo)
    git(repo, "mv", "docs/knowledge/a.md", "docs/knowledge/moved.md")
    git(repo, "commit", "-qm", "rename")
    after = source.read_snapshot(repo)
    assert before.docs[0].doc_id == after.docs[0].doc_id
    assert before.raw_hashes == after.raw_hashes
    assert after.docs[0].rel_path == "moved.md"
    assert before.snapshot_hash != after.snapshot_hash


def test_duplicate_identity_rejects_whole_snapshot(repo):
    commit_file(repo)
    commit_file(repo, "b.md")
    with pytest.raises(source.GitSourceError, match="Duplicate document_id"):
        source.read_snapshot(repo)


@pytest.mark.parametrize("raw", [
    b"no frontmatter", b"---\nscenario: vpn", b"---\n[invalid: yaml\n---",
    b"---\n- list\n---", document(extra="scenario: email\n"),
    document(extra="scenario: ''\n"), document(identity="not-a-uuid"),
    document(extra="x: &foo vpn\ny: *foo\n"),
    document(extra="x: {nested: map}\n"), document(extra="x: [1, 2]\n"),
    document(extra="x: null\n"), document(extra="x: .inf\n"),
    document(body="![image](missing.png)"), document(body="![image][ref]"),
    document(body='<img src="missing.png">'), b"\xff\xfe", document(body="\x00"),
    b"version https://git-lfs.github.com/spec/v1\noid sha256:123\nsize 5\n",
    b"---\nscenario: vpn\n---\nno id\n",
])
def test_invalid_markdown_fails(raw):
    with pytest.raises(source.GitSourceError):
        source.read_snapshot_from_files(SHA, {"unknown.md": raw})


def test_dates_are_json_safe_and_uuid_canonicalized():
    snap = source.read_snapshot_from_files(SHA, {"a.md": document(
        identity=ID.upper(), extra="valid_to: 2099-12-31\ncreated_at: 2026-01-01T12:00:00Z\ntags: [vpn, cert]\n"
    )})
    assert snap.docs[0].doc_id == ID
    assert snap.docs[0].meta["valid_to"] == "2099-12-31"
    assert snap.docs[0].meta["created_at"] == "2026-01-01T12:00:00+00:00"
    json.dumps(snap.docs[0].meta)


def test_symlink_rejected(repo):
    commit_file(repo)
    (repo / "docs/knowledge/link.md").symlink_to("a.md")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "symlink")
    with pytest.raises(source.GitSourceError, match="Symlink"):
        source.read_snapshot(repo)


def test_submodule_rejected(repo):
    sha = commit_file(repo)
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},docs/knowledge/submodule")
    git(repo, "commit", "-qm", "gitlink")
    with pytest.raises(source.GitSourceError, match="submodule"):
        source.read_snapshot(repo)


def test_subdir_symlink_rejected(repo):
    commit_file(repo)
    (repo / "linked").symlink_to("docs/knowledge")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "link directory")
    with pytest.raises(source.GitSourceError):
        source.read_snapshot(repo, subdir="linked")


@pytest.mark.parametrize("name", ["a.pdf", "a.png", "a.txt", "a.lfs"])
def test_unsupported_file_rejects_snapshot(repo, name):
    commit_file(repo, name)
    with pytest.raises(source.GitSourceError, match="Unsupported file"):
        source.read_snapshot(repo)


def test_complete_empty_snapshot(repo):
    git(repo, "commit", "--allow-empty", "-qm", "empty repo")
    snap = source.read_snapshot(repo)
    assert snap.files == {} and snap.docs == [] and snap.raw_hashes == {}
    assert snap.commit == git(repo, "rev-parse", "HEAD")


def test_failed_git_is_never_empty_snapshot(repo, monkeypatch):
    commit_file(repo)
    original = source._git

    def fail(repo_arg, *args):
        if args[0] == "ls-tree":
            raise source.GitSourceError("failed enumeration")
        return original(repo_arg, *args)

    monkeypatch.setattr(source, "_git", fail)
    with pytest.raises(source.GitSourceError, match="enumeration"):
        source.read_snapshot(repo)


@pytest.mark.parametrize("stage", ["ls-tree", "cat-file"])
def test_incomplete_git_output_fails(repo, monkeypatch, stage):
    commit_file(repo)
    original = source._git

    def truncate(repo_arg, *args):
        raw = original(repo_arg, *args)
        return raw[:-1] if args[0] == stage else raw

    monkeypatch.setattr(source, "_git", truncate)
    with pytest.raises(source.GitSourceError, match="Incomplete"):
        source.read_snapshot(repo)


def test_git_nonzero_and_timeout_errors(repo, monkeypatch):
    with pytest.raises(source.GitSourceError, match="command failed"):
        source.read_snapshot(repo, ref="missing")

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == source.GIT_TIMEOUT_SECONDS
        assert isinstance(args[0], list)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(source.subprocess, "run", timeout)
    with pytest.raises(source.GitSourceError, match="command failed"):
        source.read_snapshot(repo)


@pytest.mark.parametrize("path", ["../a.md", "/a.md", "a/../b.md", "a//b.md", "./a.md", "-a.md", "a\\b.md", "a\nb.md", ""])
def test_unsafe_paths(path):
    with pytest.raises(source.GitSourceError):
        source.read_snapshot_from_files(SHA, {path: document()})
    with pytest.raises(source.GitSourceError):
        source.read_snapshot(Path("/does-not-exist"), subdir=path)


def test_limits(monkeypatch):
    monkeypatch.setattr(source, "MAX_DOCUMENTS", 1)
    with pytest.raises(source.GitSourceError, match="Too many"):
        source.read_snapshot_from_files(SHA, {"a.md": document(), "b.md": document(ID2)})
    monkeypatch.setattr(source, "MAX_FILE_BYTES", 1)
    with pytest.raises(source.GitSourceError, match="oversized"):
        source.read_snapshot_from_files(SHA, {"a.md": document()})
    monkeypatch.setattr(source, "MAX_FILE_BYTES", 9999)
    monkeypatch.setattr(source, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(source.GitSourceError, match="byte limit"):
        source.read_snapshot_from_files(SHA, {"a.md": document()})


def test_legacy_map_is_explicit_and_rename_requires_id():
    legacy = json.loads(Path(source.__file__).with_name("legacy_document_ids.json").read_text())
    assert len(legacy) == 16 and len(set(legacy.values())) == 16
    assert all(str(UUID(value)) == value for value in legacy.values())
    path = next(iter(legacy))
    raw = b"---\nscenario: email\n---\n# Legacy\n"
    snap = source.read_snapshot_from_files(SHA, {path: raw})
    assert snap.docs[0].doc_id == legacy[path]
    with pytest.raises(source.GitSourceError, match="document_id"):
        source.read_snapshot_from_files(SHA, {"renamed.md": raw})


def test_export_roundtrip_survives_repository_removal(repo, tmp_path):
    commit_file(repo)
    snap = source.read_snapshot(repo)
    target = tmp_path / "exports/snapshot.json"
    source.write_export(snap, target)
    # Export restore doesn't consult the repository, loader, images or external APIs.
    git(repo, "update-ref", "-d", "HEAD")
    assert source.read_export(target) == snap
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("tamper", ["bytes", "sha256", "snapshot_hash", "commit", "path", "id"])
def test_export_tampering_rejected(tmp_path, tamper):
    target = tmp_path / "snapshot.json"
    snap = source.read_snapshot_from_files(SHA, {"a.md": document()})
    source.write_export(snap, target)
    payload = json.loads(target.read_text())
    if tamper in {"bytes", "sha256"}:
        payload["files"]["a.md"][tamper] = "invalid"
    elif tamper == "path":
        payload["files"]["../a.md"] = payload["files"].pop("a.md")
    elif tamper == "id":
        payload["files"]["b.md"] = payload["files"]["a.md"]
    else:
        payload[tamper] = "b" * 40
    target.write_text(json.dumps(payload))
    with pytest.raises(source.GitSourceError):
        source.read_export(target)


def test_export_duplicate_json_key_and_bad_json_fail(tmp_path):
    target = tmp_path / "snapshot.json"
    for value in ['{"version":1,"version":1}', '{', '[]']:
        target.write_text(value)
        with pytest.raises(source.GitSourceError):
            source.read_export(target)


def test_mutated_snapshot_cannot_export(tmp_path):
    snap = source.read_snapshot_from_files(SHA, {"a.md": document()})
    snap.files["a.md"] = document(body="mutated")
    with pytest.raises(source.GitSourceError, match="mutated"):
        source.write_export(snap, tmp_path / "snapshot.json")


def test_git_lfs_pointer_rejected(repo):
    commit_file(repo, raw=b"version https://git-lfs.github.com/spec/v1\noid sha256:123\nsize 5\n")
    with pytest.raises(source.GitSourceError, match="LFS"):
        source.read_snapshot(repo)


def test_git_blob_size_limits_apply_before_read(repo, monkeypatch):
    commit_file(repo)
    monkeypatch.setattr(source, "MAX_FILE_BYTES", 10)
    with pytest.raises(source.GitSourceError, match="Oversized blob"):
        source.read_snapshot(repo)


def test_failed_export_replace_keeps_previous_snapshot(tmp_path, monkeypatch):
    target = tmp_path / "snapshot.json"
    snap = source.read_snapshot_from_files(SHA, {"a.md": document()})
    source.write_export(snap, target)
    before = target.read_bytes()

    def fail_replace(*args):
        raise OSError("replace interrupted")

    monkeypatch.setattr(source.os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted"):
        source.write_export(snap, target)
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def test_nonstring_input_path_is_validation_error():
    with pytest.raises(source.GitSourceError, match="Unsafe source path"):
        source.read_snapshot_from_files(SHA, {1: document(), "a.md": document(ID2)})


def test_export_size_limit_applies_to_write_and_read(tmp_path, monkeypatch):
    target = tmp_path / "snapshot.json"
    snap = source.read_snapshot_from_files(SHA, {"a.md": document()})
    source.write_export(snap, target)
    monkeypatch.setattr(source, "MAX_EXPORT_BYTES", 10)
    with pytest.raises(source.GitSourceError, match="Export byte limit"):
        source.read_export(target)
    with pytest.raises(source.GitSourceError, match="Export byte limit"):
        source.write_export(snap, target)

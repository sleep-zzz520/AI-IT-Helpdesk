"""Persistent stage reuse with authoritative source validation and real splitting."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.rag import git_source as source
from app.rag import stage_cache as cache
from app.rag.chunks import KnowledgeDoc
from app.rag.splitter import split_markdown

ID = "d498e1ba-ac4f-4bce-9664-4b692f156bb6"
SHA = "a" * 40


@pytest.fixture(autouse=True)
def mock_llm():
    yield


def document(body="# Title\n## Fix\nKeep 42 days.", identity=ID):
    identity_line = f"document_id: {identity}\n" if identity else ""
    return f"---\nscenario: vpn\n{identity_line}tags: [vpn, cert]\n---\n{body}\n".encode()


def watch_parse(monkeypatch):
    calls = []
    real_parse = source._parse_content

    def counted(path, raw):
        calls.append(path)
        return real_parse(path, raw)

    monkeypatch.setattr(source, "_parse_content", counted)
    return calls


def test_parse_reuses_across_commit_and_rename_but_rebinds_path(tmp_path, monkeypatch):
    calls = watch_parse(monkeypatch)
    stats = {}
    first = source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path, stats=stats)
    second = source.read_snapshot_from_files("b" * 40, {"moved.md": document()}, cache_dir=tmp_path, stats=stats)
    assert calls == ["a.md"]
    assert stats == {"parse_hits": 1, "parse_misses": 1, "parsed_documents": 1}
    assert first.raw_hashes == second.raw_hashes
    assert second.docs[0].meta["source_url"] == second.docs[0].rel_path == "moved.md"
    assert second.docs[0].doc_id == ID
    assert first.snapshot_hash != second.snapshot_hash


def test_cached_legacy_parse_does_not_authorize_rename(tmp_path, monkeypatch):
    calls = watch_parse(monkeypatch)
    legacy = json.loads(Path(source.__file__).with_name("legacy_document_ids.json").read_text())
    name = next(iter(legacy))
    snap = source.read_snapshot_from_files(SHA, {name: document(identity=None)}, cache_dir=tmp_path)
    assert snap.docs[0].doc_id == legacy[name]
    with pytest.raises(source.GitSourceError, match="document_id"):
        source.read_snapshot_from_files(SHA, {"renamed.md": document(identity=None)}, cache_dir=tmp_path)
    assert calls == [name]


def test_duplicate_uuid_is_checked_even_when_both_parses_hit(tmp_path, monkeypatch):
    calls = watch_parse(monkeypatch)
    source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path)
    with pytest.raises(source.GitSourceError, match="Duplicate document_id"):
        source.read_snapshot_from_files(SHA, {"a.md": document(), "b.md": document()}, cache_dir=tmp_path)
    assert calls == ["a.md"]


def test_parser_profile_change_forces_reparse(tmp_path, monkeypatch):
    calls = watch_parse(monkeypatch)
    source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path)
    original = source.parser_profile
    monkeypatch.setattr(source, "parser_profile", lambda ids: original(ids) + "new-parser")
    source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path)
    assert calls == ["a.md", "a.md"]


def test_profile_binds_migration_configuration():
    assert source.parser_profile({"a.md": ID}) != source.parser_profile({"b.md": ID})


@pytest.mark.parametrize("raw", [b"invalid", document(identity="broken"), document(identity=None)])
def test_failed_parse_never_creates_success_artifact(tmp_path, raw):
    for _ in range(2):
        with pytest.raises(source.GitSourceError):
            source.read_snapshot_from_files(SHA, {"unknown.md": raw}, cache_dir=tmp_path)
    assert list(tmp_path.rglob("*.json")) == []


def test_cache_hit_still_checks_authoritative_limits_paths_format(tmp_path, monkeypatch):
    source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path)
    for path in ["../a.md", "a.pdf"]:
        with pytest.raises(source.GitSourceError):
            source.read_snapshot_from_files(SHA, {path: document()}, cache_dir=tmp_path)
    monkeypatch.setattr(source, "MAX_FILE_BYTES", 1)
    with pytest.raises(source.GitSourceError, match="oversized"):
        source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path)


def test_source_byte_change_reparses_and_updates_content(tmp_path, monkeypatch):
    calls = watch_parse(monkeypatch)
    source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path)
    snap = source.read_snapshot_from_files(SHA, {"a.md": document("changed 43")}, cache_dir=tmp_path)
    assert len(calls) == 2
    assert snap.docs[0].content == "changed 43\n"


@pytest.mark.parametrize("damage", ["json", "digest", "structure"])
def test_corrupt_parsed_cache_safely_recomputes(tmp_path, monkeypatch, damage):
    calls = watch_parse(monkeypatch)
    source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path)
    path = next((tmp_path / "parsed").glob("*.json"))
    value = json.loads(path.read_text())
    if damage == "json":
        path.write_text("{")
    else:
        value["payload"]["content"] = None if damage == "structure" else "wrong"
        if damage == "structure":
            value["digest"] = cache._digest(value["payload"])
        path.write_text(json.dumps(value))
    actual = source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path)
    assert actual.docs[0].content == "# Title\n## Fix\nKeep 42 days.\n"
    assert len(calls) == 2


def test_optional_cache_write_failure_does_not_fail_snapshot(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("read-only cache")

    monkeypatch.setattr(cache, "_write", fail)
    snap = source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path)
    assert snap.docs[0].doc_id == ID


def test_no_cache_keeps_parsing_and_creates_nothing(tmp_path, monkeypatch):
    calls = watch_parse(monkeypatch)
    for _ in range(2):
        source.read_snapshot_from_files(SHA, {"a.md": document()})
    assert len(calls) == 2
    assert list(tmp_path.iterdir()) == []


def test_export_checksum_validation_precedes_parse_cache(tmp_path):
    snapshot = source.read_snapshot_from_files(SHA, {"a.md": document()}, cache_dir=tmp_path / "cache")
    path = tmp_path / "export.json"
    source.write_export(snapshot, path)
    stats = {}
    assert source.read_export(path, cache_dir=tmp_path / "cache", stats=stats) == snapshot
    assert stats == {"parse_hits": 1}
    value = json.loads(path.read_text())
    value["files"]["a.md"]["sha256"] = "0" * 64
    path.write_text(json.dumps(value))
    with pytest.raises(source.GitSourceError, match="checksum"):
        source.read_export(path, cache_dir=tmp_path / "cache")


def test_git_entrypoint_reads_raw_objects_even_on_parse_hit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Cache tests")
    git("config", "user.email", "cache@example.invalid")
    path = repo / "docs/knowledge/a.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(document())
    git("add", ".")
    git("commit", "-qm", "source")
    calls = watch_parse(monkeypatch)
    cache_dir = tmp_path / "cache"
    source.read_snapshot(repo, cache_dir=cache_dir)
    git("commit", "--allow-empty", "-qm", "new commit")
    stats = {}
    source.read_snapshot(repo, cache_dir=cache_dir, stats=stats)
    assert len(calls) == 1 and stats == {"parse_hits": 1}
    original = source._git

    def incomplete(repo, *args):
        raw = original(repo, *args)
        return raw[:-1] if args[0] == "cat-file" else raw

    monkeypatch.setattr(source, "_git", incomplete)
    with pytest.raises(source.GitSourceError, match="Incomplete"):
        source.read_snapshot(repo, cache_dir=cache_dir)


@pytest.fixture
def chunk_input():
    doc = source.read_snapshot_from_files(SHA, {"a.md": document()}).docs[0]
    return doc


def splitter_builder(doc, calls, revision="rev", source_id="source"):
    def build():
        calls.append(doc.rel_path)
        result = split_markdown(doc)
        chunks = [result.parent, *result.children]
        for chunk in chunks:
            chunk.metadata.update(source_id=source_id, doc_revision=revision)
        return chunks
    return build


def test_chunks_reuse_persisted_complete_split_and_are_mutation_safe(tmp_path, chunk_input):
    calls, stats = [], {}
    builder = splitter_builder(chunk_input, calls)
    first = cache.cached_chunks(chunk_input, "rev", "source", "split-v1", builder, cache_dir=tmp_path, stats=stats)
    first[0].metadata["scenario"] = "mutated"
    second = cache.cached_chunks(chunk_input, "rev", "source", "split-v1", builder, cache_dir=tmp_path, stats=stats)
    assert calls == ["a.md"]
    assert stats == {"chunk_hits": 1, "chunk_misses": 1, "chunked_documents": 1}
    assert [c.metadata["chunk_type"] for c in second] == ["parent", "child"]
    assert second[0].text == chunk_input.content and second[0].metadata["scenario"] == "vpn"
    assert second[1].metadata["parent_id"] == second[0].id


@pytest.mark.parametrize("changed", ["body", "meta", "path", "revision", "profile", "source", "id"])
def test_chunk_key_binds_all_semantic_inputs(tmp_path, chunk_input, changed):
    calls = []
    cache.cached_chunks(chunk_input, "rev", "source", "v1", splitter_builder(chunk_input, calls), cache_dir=tmp_path)
    doc = KnowledgeDoc(chunk_input.rel_path, chunk_input.doc_id, dict(chunk_input.meta), chunk_input.content)
    revision, source_id, profile = "rev", "source", "v1"
    if changed == "body":
        doc.content += "Updated 43 days"
    elif changed == "meta":
        doc.meta["scenario"] = "email"
    elif changed == "path":
        doc.rel_path = "renamed.md"
    elif changed == "revision":
        revision = "rev2"
    elif changed == "source":
        source_id = "another"
    elif changed == "profile":
        profile = "v2"
    else:
        doc.doc_id = "another-id"
    cache.cached_chunks(doc, revision, source_id, profile,
                        splitter_builder(doc, calls, revision, source_id), cache_dir=tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize("damage", ["digest", "metadata", "parent", "duplicate", "missing-parent"])
def test_chunk_corruption_recomputes_actual_split(tmp_path, chunk_input, damage):
    calls = []
    builder = splitter_builder(chunk_input, calls)
    expected = cache.cached_chunks(chunk_input, "rev", "source", "v1", builder, cache_dir=tmp_path)
    path = next((tmp_path / "chunks").glob("*.json"))
    value = json.loads(path.read_text())
    if damage == "digest":
        value["payload"][1]["text"] = "corrupted"
    else:
        if damage == "metadata":
            value["payload"][1]["metadata"]["doc_revision"] = "stale"
        elif damage == "parent":
            value["payload"][0]["text"] = "corrupted"
        elif damage == "duplicate":
            value["payload"].append(value["payload"][1])
        else:
            value["payload"].pop(0)
        value["digest"] = cache._digest(value["payload"])
    path.write_text(json.dumps(value))
    assert cache.cached_chunks(chunk_input, "rev", "source", "v1", builder, cache_dir=tmp_path) == expected
    assert len(calls) == 2


def test_split_failure_never_persists_success(tmp_path, chunk_input):
    def fail():
        raise RuntimeError("split interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        cache.cached_chunks(chunk_input, "rev", "source", "v1", fail, cache_dir=tmp_path)
    assert list(tmp_path.rglob("*.json")) == []


def test_source_module_is_portable_without_application_or_model_imports():
    subprocess.run([sys.executable, "-c", (
        "import sys; from app.rag.git_source import read_snapshot_from_files; "
        "assert 'app.config' not in sys.modules; assert 'openai' not in sys.modules"
    )], cwd=Path(source.__file__).parents[2], check=True)


def test_interrupted_cache_replace_preserves_previous_artifact(tmp_path, monkeypatch):
    path = tmp_path / "parsed/value.json"
    before = {"previous": "known-good"}
    cache._write(path, before)

    def fail(*args):
        raise OSError("interrupted replace")

    monkeypatch.setattr(cache.os, "replace", fail)
    with pytest.raises(OSError, match="interrupted"):
        cache._write(path, {"replacement": "new"})
    assert json.loads(path.read_text()) == before
    assert list(path.parent.iterdir()) == [path]

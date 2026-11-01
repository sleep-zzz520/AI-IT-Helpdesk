"""Real export -> enqueue -> SQLite/Chroma -> publish stage-cache measurements.

Only embedding API calls are deterministic local stubs. Parser/splitter spies call
real implementations and count every invocation, including export persistence.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.rag import generations as g
from app.rag import git_source as source
from app.rag import stage_cache
from tests.test_generations import A, B, raw
from tests.test_generations import env as _generation_env


@pytest.fixture(autouse=True)
def mock_llm():
    yield


@pytest.fixture
def pipeline(request, monkeypatch, tmp_path):
    env = request.getfixturevalue("_generation_env")
    export = tmp_path / "input-export.json"
    monkeypatch.setattr(settings, "KB_GIT_EXPORT", str(export))
    parsed, split = [], []
    original_parse, original_split = source._parse_content, g.split_markdown

    def parse(path, contents):
        parsed.append(path)
        return original_parse(path, contents)

    def chunk(doc):
        split.append(doc.rel_path)
        return original_split(doc)

    monkeypatch.setattr(source, "_parse_content", parse)
    monkeypatch.setattr(g, "split_markdown", chunk)

    def input_export(version=1, *, body="VPN alpha unique evidence", path="a.md", **meta):
        snap = source.read_snapshot_from_files(f"{version:040x}", {
            path: raw(A, body, **meta), "b.md": raw(B, "VPN beta unique evidence"),
        })
        source.write_export(snap, export)
        # Fixture preparation is deliberately outside the production call counts.
        parsed.clear()
        split.clear()
        env.calls.clear()

    def prepare():
        job = g.enqueue()  # Real configured_snapshot reads the temporary source export.
        result = g.process_job(job["id"])
        assert result["status"] == "prepared", result
        return result

    def publish(job, *, cached=True):
        before = (len(parsed), len(split), len(env.calls))
        result = g.publish(job["id"])
        assert result["status"] == "published", result
        if cached:
            assert (len(parsed), len(split), len(env.calls)) == before
        return result

    return SimpleNamespace(env=env, input=input_export, prepare=prepare, publish=publish,
                           parsed=parsed, split=split, calls=env.calls, patch=monkeypatch)


def test_real_export_cold_warm_and_one_edit_absolute_stage_counts(pipeline):
    pipeline.input()
    first = pipeline.prepare()
    assert pipeline.parsed == ["a.md", "b.md"]
    assert pipeline.split == ["a.md", "b.md"]
    assert first["report"]["discovery"] == {"parse_misses": 2, "parsed_documents": 2}
    assert first["report"]["stages"] == {
        "parse_hits": 2, "chunk_misses": 2, "chunked_documents": 2,
    }
    assert len(pipeline.calls) == first["report"]["embedding_inputs"] == 2
    pipeline.publish(first)

    pipeline.input(2)
    warm = pipeline.prepare()
    assert pipeline.parsed == pipeline.split == pipeline.calls == []
    assert warm["report"]["discovery"] == {"parse_hits": 2}
    assert warm["report"]["stages"] == {"parse_hits": 2, "chunk_hits": 2}
    assert warm["report"]["skipped_count"] == 2
    assert warm["report"]["embedding_inputs"] == 0
    pipeline.publish(warm)

    pipeline.input(3, body="VPN alpha changed certificate renewal 720")
    changed = pipeline.prepare()
    assert pipeline.parsed == pipeline.split == ["a.md"]
    assert pipeline.calls == ["VPN alpha changed certificate renewal 720"]
    assert changed["report"]["discovery"] == {
        "parse_hits": 1, "parse_misses": 1, "parsed_documents": 1,
    }
    assert changed["report"]["stages"] == {
        "parse_hits": 2, "chunk_hits": 1, "chunk_misses": 1, "chunked_documents": 1,
    }
    assert changed["report"]["embedding_inputs"] == 1
    pipeline.publish(changed)
    content = " ".join(g.resolve_store().get_all().values())
    assert "renewal 720" in content and "alpha unique" not in content


@pytest.mark.parametrize("change", ["metadata", "rename"])
def test_semantic_attribute_changes_rebuild_only_affected_chunks_reusing_vectors(pipeline, change):
    pipeline.input()
    pipeline.publish(pipeline.prepare())
    pipeline.input(2, **({"title": "Reviewed certificate guide"} if change == "metadata"
                         else {"path": "renamed/a.md"}))
    job = pipeline.prepare()
    assert pipeline.parsed == (["a.md"] if change == "metadata" else [])
    assert pipeline.split == (["a.md"] if change == "metadata" else ["renamed/a.md"])
    assert pipeline.calls == [] and job["report"]["embedding_inputs"] == 0
    pipeline.publish(job)
    records = [item["metadata"] for item in g.resolve_store().get_all_meta().values()
               if item["metadata"]["doc_id"] == A]
    assert records
    if change == "metadata":
        assert all(meta["title"] == "Reviewed certificate guide" for meta in records)
    else:
        assert all(meta["source_url"] == "docs/knowledge/renamed/a.md" for meta in records)


def test_parser_code_profile_invalidates_parses_but_preserves_same_embedding_inputs(pipeline):
    pipeline.input()
    pipeline.publish(pipeline.prepare())
    pipeline.input(2)
    original_parser_profile = source.parser_profile
    old_embedding_profile, old_pipeline_profile = g.embedding_profile(), g.pipeline_profile()
    # Simulate a deployed parser-code fingerprint change without editing source files.
    pipeline.patch.setattr(source, "parser_profile", lambda ids: original_parser_profile(ids) + "-v2")
    assert g.pipeline_profile() != old_pipeline_profile
    job = pipeline.prepare()
    assert g.embedding_profile() == old_embedding_profile
    assert pipeline.parsed == ["a.md", "b.md"] and pipeline.split == []
    assert pipeline.calls == [] and job["report"]["embedding_inputs"] == 0
    assert job["report"]["discovery"]["parsed_documents"] == 2
    assert job["report"]["reused_vectors"] == 2
    pipeline.publish(job)


def test_splitter_profile_change_rebuilds_chunks_without_reparsing_or_reembedding(pipeline):
    pipeline.input()
    pipeline.publish(pipeline.prepare())
    pipeline.input(2)
    original = g.chunk_profile()
    pipeline.patch.setattr(g, "chunk_profile", lambda: original + "-splitter-v2")
    job = pipeline.prepare()
    assert pipeline.parsed == [] and pipeline.split == ["a.md", "b.md"]
    assert pipeline.calls == [] and job["report"]["embedding_inputs"] == 0
    pipeline.publish(job)


def test_corrupted_parse_and_chunk_files_recompute_in_real_pipeline(pipeline):
    pipeline.input()
    pipeline.publish(pipeline.prepare())
    stage_root = g.stage_cache_dir()
    corrupted = list(stage_root.rglob("*.json"))
    assert len(corrupted) == 4
    for path in corrupted:
        path.write_text("{interrupted cache")
    pipeline.input(2)
    job = pipeline.prepare()
    assert pipeline.parsed == pipeline.split == ["a.md", "b.md"]
    assert pipeline.calls == [] and job["report"]["embedding_inputs"] == 0
    pipeline.publish(job)


def test_unwritable_optional_stage_cache_still_builds_and_publishes(pipeline):
    pipeline.input()

    def fail(*args):
        raise OSError("simulated read-only stage cache")

    pipeline.patch.setattr(stage_cache, "_write", fail)
    job = pipeline.prepare()
    assert len(pipeline.calls) == job["report"]["embedding_inputs"] == 2
    assert not list(g.stage_cache_dir().rglob("*.json"))
    pipeline.publish(job, cached=False)
    assert len(g.resolve_store().get_all()) == 4


def test_source_namespaces_isolate_parsed_chunks_vectors_and_published_metadata(pipeline):
    pipeline.input()
    first_root = g.stage_cache_dir()
    pipeline.publish(pipeline.prepare())
    first_files = {path: path.read_bytes() for path in first_root.rglob("*.json")}
    pipeline.input(2)
    pipeline.patch.setattr(settings, "KB_SOURCE_ID", "another-test-source")
    second_root = g.stage_cache_dir()
    assert second_root != first_root
    job = pipeline.prepare()
    assert pipeline.parsed == pipeline.split == ["a.md", "b.md"]
    assert len(pipeline.calls) == job["report"]["embedding_inputs"] == 2
    pipeline.publish(job)
    assert {item["metadata"]["source_id"] for item in g.resolve_store().get_all_meta().values()} == {
        "another-test-source",
    }
    assert all(path.read_bytes() == contents for path, contents in first_files.items())
    assert len(list(second_root.rglob("*.json"))) == 4

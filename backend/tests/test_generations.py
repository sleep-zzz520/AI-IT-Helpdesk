"""Generation safety and incremental reuse against real SQLite and Chroma storage."""
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from chromadb.errors import NotFoundError

from app import db as db_module
from app.config import settings
from app.models import KbSource, KbSyncJob
from app.rag import embedder
from app.rag import generations as g
from app.rag.bm25 import Bm25Index
from app.rag.git_source import read_snapshot_from_files
from app.rag.store import ChromaStore

A = "87103426-6789-4a36-8dc5-e38f82e5af9a"
B = "19b0e2bb-e111-422a-b589-8340a93ffac3"


@pytest.fixture(autouse=True)
def mock_llm():
    """Source/generation tests only; no application or model fixture imports."""
    yield


def raw(identity, body, **metadata):
    metadata = {"scenario": "vpn", "document_id": identity, "status": "active",
                "valid_to": "2099-12-31", **metadata}
    import yaml
    return ("---\n" + yaml.safe_dump(metadata) + "---\n" + body).encode()


def snapshot(version=1, *, a="VPN alpha unique evidence", b="VPN beta unique evidence", path="a.md", **meta):
    files = {}
    if a is not None:
        files[path] = raw(A, a, **meta)
    if b is not None:
        files["b.md"] = raw(B, b)
    return read_snapshot_from_files(f"{version:040x}", files)


@pytest.fixture
def env(db_session, monkeypatch, tmp_path):
    for name, value in {
        "KB_SYNC_MODE": "git", "KB_SOURCE_ID": "test-generations",
        "KB_SYNC_STATE_DIR": str(tmp_path / "state"),
        "KB_VECTOR_DIR": str(tmp_path / "vectors"),
        "KB_ACTIVE_FILE": tmp_path / "active.txt", "KB_PIPELINE_EPOCH": "test-v1",
        "KB_GIT_SUBDIR": "docs/knowledge", "KB_GIT_EXPORT": "",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(embedder, "EMBED_DIM", 3)
    calls = []

    def deterministic(texts):
        calls.extend(texts)
        return [[(hashlib.sha256(text.encode()).digest()[i] + 1) / 256 for i in range(3)]
                for text in texts]

    monkeypatch.setattr(embedder, "embed_texts", deterministic)

    def prepare(value=None):
        job = g.enqueue(value or snapshot())
        built = g.process_job(job["id"])
        assert built["status"] == "prepared", built
        return built

    def publish(value=None):
        return g.publish(prepare(value)["id"])

    def active():
        with db_module.SessionLocal() as db:
            source = db.get(KbSource, settings.KB_SOURCE_ID)
            return source.active_job if source else None

    return SimpleNamespace(calls=calls, prepare=prepare, publish=publish, active=active,
                           session=db_session, embed=deterministic, patch=monkeypatch)


def test_initial_prepare_publish_is_persistent_and_idempotent(env):
    value = snapshot()
    queued = g.enqueue(value)
    assert queued["status"] == "queued" and env.active() is None
    assert g.enqueue(value)["id"] == queued["id"]
    prepared = g.process_job(queued["id"])
    assert prepared["status"] == "prepared", prepared
    assert env.active() is None
    assert len(env.calls) == 2
    published = g.publish(queued["id"])
    assert published["status"] == "published" and env.active() == queued["id"]
    assert g.enqueue(value)["id"] == queued["id"]
    assert g.process_job(queued["id"])["status"] == "published"
    assert g.publish(queued["id"])["id"] == queued["id"]
    assert len(env.calls) == 2
    store = g.resolve_store()
    assert {item["metadata"]["doc_id"] for item in store.get_all_meta().values()} == {A, B}
    assert {hit.metadata["doc_id"] for hit in store.query([1, 1, 1], 10)} == {A, B}


def test_only_changed_document_embeds_new_inputs(env):
    env.publish()
    env.calls.clear()
    updated = env.prepare(snapshot(2, a="VPN alpha changed certificate renewal 720"))
    assert env.calls == ["VPN alpha changed certificate renewal 720"]
    assert updated["report"]["updated"] == ["a.md"]
    assert updated["report"]["skipped_count"] == 1
    assert updated["report"]["embedding_inputs"] == 1
    g.publish(updated["id"])
    text = " ".join(g.resolve_store().get_all().values())
    assert "changed certificate" in text and "alpha unique" not in text


@pytest.mark.parametrize("kind", ["metadata", "rename"])
def test_metadata_and_rename_reuse_vectors_but_publish_current_attributes(env, kind):
    env.publish()
    env.calls.clear()
    value = snapshot(2, **({"title": "New visible title"} if kind == "metadata" else {"path": "renamed/a.md"}))
    job = env.prepare(value)
    assert env.calls == []
    assert job["report"]["embedding_inputs"] == 0
    g.publish(job["id"])
    metas = [item["metadata"] for item in g.resolve_store().get_all_meta().values()
             if item["metadata"]["doc_id"] == A]
    assert metas
    if kind == "metadata":
        assert all(meta["title"] == "New visible title" for meta in metas)
    else:
        assert all(meta["source_url"].endswith("renamed/a.md") for meta in metas)


def test_deleted_document_is_denied_even_when_other_embedding_fails(env):
    old = env.publish()
    old_store = g.resolve_store()
    old_bm25 = Bm25Index(old_store)
    old_bm25._ensure_built()
    deleted_ids = old_store.get_doc_ids(A)
    assert deleted_ids
    job = g.enqueue(snapshot(2, a=None, b="changed beta requiring embedding"))

    def unavailable(_):
        raise RuntimeError("embedding service unavailable")

    env.patch.setattr(embedder, "embed_texts", unavailable)
    assert g.process_job(job["id"])["status"] == "failed"
    assert env.active() == old["id"]
    assert A in g.denied_ids()
    for store in [old_store, g.resolve_store()]:
        assert not store.get_by_ids(deleted_ids)
        assert all(hit.metadata["doc_id"] != A for hit in store.query([1, 1, 1], 10))
        assert all(item["metadata"]["doc_id"] != A for item in store.get_all_meta().values())
    hits = old_bm25.search("alpha unique evidence", 10)
    # A preexisting BM25 object must respect authoritative deletion on delivery.
    from app.rag.store import Hit
    assert all(hit.metadata["doc_id"] != A for hit in g.filter_hits([
        Hit(cid, text, meta, score) for cid, text, score, meta in hits]))


def test_rollback_cannot_resurrect_deleted_document(env):
    first = env.publish()
    second = env.publish(snapshot(2, a=None))
    assert env.active() == second["id"]
    g.rollback()
    assert env.active() == first["id"]
    assert A in g.denied_ids()
    assert all(hit.metadata["doc_id"] != A for hit in g.resolve_store().query([1, 1, 1], 10))
    assert {item["metadata"]["doc_id"] for item in g.resolve_store().get_all_meta().values()} == {B}


def test_old_prepared_task_cannot_publish_after_new_intent(env):
    old = env.prepare()
    new = g.enqueue(snapshot(2, a="new alpha"))
    with pytest.raises(ValueError, match="过期"):
        g.publish(old["id"])
    assert env.active() is None
    assert g.process_job(new["id"])["status"] == "prepared"
    g.publish(new["id"])
    with pytest.raises(ValueError):
        g.publish(old["id"])


def test_task_finishing_after_newer_enqueue_is_superseded(env):
    old = g.enqueue(snapshot())
    invoked = False

    def enqueue_during_embedding(texts):
        nonlocal invoked
        if not invoked:
            invoked = True
            g.enqueue(snapshot(2, a="newer desired alpha"))
        return env.embed(texts)

    env.patch.setattr(embedder, "embed_texts", enqueue_during_embedding)
    result = g.process_job(old["id"])
    assert result["status"] == "superseded", result
    with pytest.raises(ValueError):
        g.publish(old["id"])
    assert env.active() is None


@pytest.mark.parametrize("stage", ["queued", "prepared"])
def test_profile_change_rejects_build_or_publication(env, stage):
    job = env.prepare() if stage == "prepared" else g.enqueue(snapshot())
    env.patch.setattr(settings, "KB_PIPELINE_EPOCH", "changed-profile")
    if stage == "queued":
        result = g.process_job(job["id"])
        assert result["status"] == "failed" and "配置已变化" in result["error"]
        assert env.calls == []
    else:
        with pytest.raises(ValueError):
            g.publish(job["id"])
    assert env.active() is None


def test_partial_upsert_failure_keeps_active_generation_intact(env):
    first = env.publish()
    old_ids = set(g.resolve_store().get_all())
    job = g.enqueue(snapshot(2, a="changed alpha", b="changed beta"))
    original = ChromaStore.upsert_chunks

    def partial_failure(store, chunks, vectors):
        original(store, chunks[:1], vectors[:1])
        raise OSError("simulated storage failure after partial upsert")

    env.patch.setattr(ChromaStore, "upsert_chunks", partial_failure)
    result = g.process_job(job["id"])
    assert result["status"] == "failed" and result["error_kind"] == "storage_error"
    assert result["failures"][-1]["stage"] == "index_write"
    assert env.active() == first["id"]
    assert set(g.resolve_store().get_all()) == old_ids
    with pytest.raises(ValueError):
        g.publish(job["id"])
    with pytest.raises(RuntimeError):
        g.resolve_store(job["collection"])


def test_expired_running_lease_recovers_and_old_fence_cannot_renew(env):
    job = g.enqueue(snapshot())
    with db_module.SessionLocal.begin() as db:
        row = db.get(KbSyncJob, job["id"])
        row.status, row.fence, row.attempts = "running", 7, 1
        row.lease_until = g._now() - timedelta(seconds=1)
    result = g.process_job(job["id"])
    assert result["status"] == "prepared" and result["attempts"] == 2, result
    with db_module.SessionLocal() as db:
        assert db.get(KbSyncJob, job["id"]).fence == 8
    with pytest.raises(RuntimeError, match="租约"):
        g._lease(job["id"], 7)
    g.publish(job["id"])


def test_live_running_lease_cannot_be_double_claimed(env):
    job = g.enqueue(snapshot())
    with db_module.SessionLocal.begin() as db:
        row = db.get(KbSyncJob, job["id"])
        row.status, row.fence, row.attempts = "running", 1, 1
        row.lease_until = g._now() + timedelta(seconds=100)
    assert g.process_job(job["id"])["status"] == "running"
    assert not env.calls


def test_active_collection_loss_fails_closed_without_recreating(env):
    published = env.publish()
    store = g.resolve_store()
    store._client.delete_collection(published["collection"])
    with pytest.raises((RuntimeError, NotFoundError)):
        g.resolve_store()
    assert all(c.name != published["collection"] for c in store._client.list_collections())
    assert env.active() == published["id"]


def test_active_generation_missing_chunk_fails_closed(env):
    published = env.publish()
    raw_store = ChromaStore(settings.KB_VECTOR_DIR, published["collection"], must_exist=True)
    raw_store.delete_ids([next(iter(raw_store.get_all()))])
    with pytest.raises(RuntimeError, match="完整"):
        g.resolve_store()


def test_rollback_refuses_damaged_old_generation(env):
    first = env.publish()
    second = env.publish(snapshot(2, a="updated alpha"))
    old = ChromaStore(settings.KB_VECTOR_DIR, first["collection"], must_exist=True)
    old.delete_ids([next(iter(old.get_all()))])
    with pytest.raises(RuntimeError, match="校验"):
        g.rollback()
    assert env.active() == second["id"]


def test_publish_refuses_corrupted_text_even_when_ids_and_ready_match(env):
    from app.rag.chunks import Chunk
    job = env.prepare()
    raw_store = ChromaStore(settings.KB_VECTOR_DIR, job["collection"], must_exist=True)
    cid, item = next(iter(raw_store.get_all_meta().items()))
    raw_store.upsert_chunks([Chunk(cid, "corrupted text", item["metadata"])], [[1, 0, 0]])
    with pytest.raises(RuntimeError, match="校验"):
        g.publish(job["id"])
    assert env.active() is None


def test_corrupt_cached_vector_is_reembedded_instead_of_reused(env):
    from pathlib import Path
    env.publish()
    files = list((Path(settings.KB_SYNC_STATE_DIR) / "embeddings").rglob("*.json"))
    for path in files:
        cached = json.loads(path.read_text())
        cached["vector"][0] += 1
        path.write_text(json.dumps(cached))
    env.calls.clear()
    job = env.prepare(snapshot(2, title="new metadata"))
    assert job["status"] == "prepared"
    assert len(env.calls) == 1  # B未变直接复制旧代；A新metadata需访问缓存，损坏则重算。


def test_long_tail_edit_embeds_tail_and_preserves_full_parent(env):
    body = "# Long document\n" + "证书续期步骤0123456789" * 220 + "TAIL_OLD"
    env.publish(snapshot(a=body, b=None))
    env.calls.clear()
    changed = body.replace("TAIL_OLD", "TAIL_NEW_NEGATION_DO_NOT_DELETE")
    job = env.prepare(snapshot(2, a=changed, b=None))
    assert len(env.calls) == 1
    assert "TAIL_NEW_NEGATION_DO_NOT_DELETE" in env.calls[0]
    assert all(len(text) <= embedder.EMBED_MAX_CHARS for text in env.calls)
    g.publish(job["id"])
    records = g.resolve_store().get_all_meta().values()
    parents = [item["text"] for item in records if item["metadata"]["chunk_type"] == "parent"]
    assert parents == [changed]
    assert any("TAIL_NEW_NEGATION_DO_NOT_DELETE" in item["text"] for item in records
               if item["metadata"]["chunk_type"] == "child")


def test_empty_source_rejected_without_changing_desired_generation(env):
    first = env.publish()
    with db_module.SessionLocal() as db:
        before = db.get(KbSource, settings.KB_SOURCE_ID).desired_seq
    with pytest.raises(ValueError, match="空来源"):
        g.enqueue(snapshot(2, a=None, b=None))
    with db_module.SessionLocal() as db:
        assert db.get(KbSource, settings.KB_SOURCE_ID).desired_seq == before
    assert env.active() == first["id"]


def test_corrupt_persisted_snapshot_fails_without_embedding(env):
    job = g.enqueue(snapshot())
    with db_module.SessionLocal() as db:
        path = db.get(KbSyncJob, job["id"]).snapshot_path
    from pathlib import Path
    Path(path).write_text('{"corrupt":"export"}')
    result = g.process_job(job["id"])
    assert result["status"] == "failed" and env.calls == []
    assert env.active() is None


def test_corrupt_manifest_prevents_publish(env):
    job = env.prepare()
    with db_module.SessionLocal.begin() as db:
        row = db.get(KbSyncJob, job["id"])
        manifest = json.loads(row.manifest_json)
        manifest["digest"] = "bad"
        row.manifest_json = json.dumps(manifest)
    with pytest.raises(RuntimeError, match="摘要"):
        g.publish(job["id"])
    assert env.active() is None


def test_restored_source_is_denied_until_complete_publication(env):
    env.publish()
    env.publish(snapshot(2, a=None))
    restored = g.enqueue(snapshot(3, a="restored alpha reviewed content"))
    assert A in g.denied_ids()
    assert all(hit.metadata["doc_id"] != A for hit in g.resolve_store().query([1, 1, 1], 10))
    built = g.process_job(restored["id"])
    assert built["status"] == "prepared", built
    assert A in g.denied_ids()
    g.publish(restored["id"])
    assert A not in g.denied_ids()
    assert {item["metadata"]["doc_id"] for item in g.resolve_store().get_all_meta().values()} == {A, B}


def test_new_pipeline_profile_uses_separate_embedding_cache(env):
    env.publish()
    env.calls.clear()
    env.patch.setattr(settings, "KB_PIPELINE_EPOCH", "new-profile-cache-space")
    built = env.prepare(snapshot(2))
    assert len(env.calls) == 2
    assert built["report"]["embedding_inputs"] == 2
    assert built["report"]["skipped_count"] == 0

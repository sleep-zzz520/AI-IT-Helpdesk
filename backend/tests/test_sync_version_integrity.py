"""稳定块ID不能代替版本校验，模拟向量库静默部分写入。"""
import pytest

from app.rag import sync
from app.rag.chunks import KnowledgeDoc
from app.rag.store import ChromaStore


def test_partial_upsert_with_same_ids_does_not_pass_version_check(tmp_path, monkeypatch):
    store = ChromaStore(str(tmp_path / "chroma"), "version_integrity")
    monkeypatch.setattr(sync, "embed_texts", lambda texts: [[1., 0.] for _ in texts])
    doc = KnowledgeDoc("a.md", "doc", {"scenario": "vpn"}, "# A\n\n## One\nold\n\n## Two\nold second")
    sync._sync_doc(doc, "old-hash", store, sync.SyncReport())
    real_upsert = store.upsert_chunks

    def partial(chunks, vectors):
        real_upsert(chunks[:1], vectors[:1])

    monkeypatch.setattr(store, "upsert_chunks", partial)
    doc.content = "# A\n\n## One\nnew\n\n## Two\nnew second"
    with pytest.raises(RuntimeError, match="不完整"):
        sync._sync_doc(doc, "new-hash", store, sync.SyncReport())
    assert any(item["metadata"]["doc_hash"] == "old-hash" for item in store.get_all_meta().values())

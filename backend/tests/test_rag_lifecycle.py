"""知识库 reconcile 生命周期测试（离线，临时 Chroma + 内存 SQLite）。

把原 ``scripts/test_rag_sync.py`` 的六组人工脚本断言纳入主 pytest：
新增、修改、幂等、软删除、物理删除、复活。嵌入调用被替换成确定性伪向量，
测试目标是同步状态和检索过滤，不产生 API 成本。
"""
import hashlib

from app.config import settings
from app.models import KbDocument
from app.rag.store import ChromaStore

DOC_A = """---
scenario: vpn
doc_title: 测试文档 A
error_codes: ["800"]
risk: low
action: vpn.renew_certificate
version: "1.0"
status: active
valid_to: "2099-12-31"
source_url: docs/knowledge/vpn/test-a.md
---

# 测试文档 A

## 症状

VPN 报错 800 且证书过期。

## 处理

执行证书续期操作。
"""

DOC_A_V2 = DOC_A.replace(
    'doc_title: 测试文档 A', 'doc_title: 测试文档 A（v2 修正）'
).replace('version: "1.0"', 'version: "2.0"').replace(
    "执行证书续期操作。", "执行证书续期操作，并通知用户重新拨号验证。（v2 新增步骤）"
)

DOC_B = """---
scenario: email
doc_title: 测试文档 B
error_codes: []
risk: low
action: email.check_config
version: "1.0"
status: active
valid_to: "2099-12-31"
source_url: docs/knowledge/email/test-b.md
---

# 测试文档 B

## 症状

邮箱无法收发。

## 处理

检查 IMAP/SMTP 配置。
"""

DOC_B_INACTIVE = DOC_B.replace("status: active", "status: inactive")


def _fake_vec(text: str, dim: int = 8) -> list[float]:
    digest = hashlib.sha256(text.encode()).hexdigest()
    return [int(digest[i * 2:i * 2 + 2], 16) / 255 - 0.5 for i in range(dim)]


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_sync_lifecycle_is_idempotent_and_auditable(
    tmp_path, db_session, monkeypatch
):
    import app.db as db_module
    import app.rag.retriever as retriever
    import app.rag.sync as sync

    kb_root = tmp_path / "docs"
    store = ChromaStore(
        persist_dir=str(tmp_path / "chroma"),
        collection_name="kb_test",
    )
    embed_calls = []

    def fake_embed_texts(texts):
        embed_calls.append(list(texts))
        return [_fake_vec(text) for text in texts]

    # sync.py 为保持运行期开销低，在模块导入时缓存了依赖；测试需要
    # 显式替换这两个缓存引用，才能把所有台账操作和嵌入调用隔离掉。
    monkeypatch.setattr(sync, "SessionLocal", db_module.SessionLocal)
    monkeypatch.setattr(sync, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(retriever, "embed_text", _fake_vec)
    monkeypatch.setattr(settings, "KB_VECTOR_DIR", str(tmp_path / "chroma"))
    retriever._BM25_CACHE.clear()

    _write(kb_root / "vpn" / "test-a.md", DOC_A)
    _write(kb_root / "email" / "test-b.md", DOC_B)

    first = sync.run_sync(kb_root, store, kb_name="lifecycle")
    assert sorted(first.added) == ["email/test-b.md", "vpn/test-a.md"]
    assert not first.failed
    assert store.count() == 6  # 两篇文档各 1 个父块 + 2 个子块

    hits = retriever.retrieve(
        "证书过期 800 续期", scenario="vpn", top_k=3, store=store
    )
    assert any(
        hit.metadata.get("source_url") == "docs/knowledge/vpn/test-a.md"
        for hit in hits
    )
    assert all(
        hit.metadata.get("chunk_type") == "child" for hit in hits
    ), "父块只应作为命中子块的补充上下文，不能参与检索排序"
    assert any(hit.parent_text for hit in hits), "命中子块后应补齐父块上下文"

    _write(kb_root / "vpn" / "test-a.md", DOC_A_V2)
    updated = sync.run_sync(kb_root, store, kb_name="lifecycle")
    assert updated.updated == ["vpn/test-a.md"]
    assert "重新拨号验证" in "\n".join(store.get_all().values())
    assert "执行证书续期操作。\n" not in "\n".join(store.get_all().values())

    calls_after_update = len(embed_calls)
    repeated = sync.run_sync(kb_root, store, kb_name="lifecycle")
    assert sorted(repeated.skipped) == ["email/test-b.md", "vpn/test-a.md"]
    assert not repeated.added and not repeated.updated
    assert len(embed_calls) == calls_after_update, "幂等同步不应重复嵌入"

    _write(kb_root / "email" / "test-b.md", DOC_B_INACTIVE)
    inactive = sync.run_sync(kb_root, store, kb_name="lifecycle")
    assert inactive.updated == ["email/test-b.md"]
    assert not retriever.retrieve(
        "邮箱无法收发", scenario="email", top_k=3, store=store
    )
    ledger = db_session.query(KbDocument).filter_by(
        kb_name="lifecycle", path="email/test-b.md"
    ).one()
    assert ledger.status == "inactive"

    (kb_root / "email" / "test-b.md").unlink()
    deleted = sync.run_sync(kb_root, store, kb_name="lifecycle")
    assert deleted.deleted == ["email/test-b.md"]
    db_session.refresh(ledger)  # sync 使用独立 Session，显式刷新本地 ORM 快照
    assert ledger.status == "removed"

    _write(kb_root / "email" / "test-b.md", DOC_B)
    revived = sync.run_sync(kb_root, store, kb_name="lifecycle")
    assert revived.added == ["email/test-b.md"]
    assert retriever.retrieve(
        "邮箱无法收发", scenario="email", top_k=3, store=store
    )

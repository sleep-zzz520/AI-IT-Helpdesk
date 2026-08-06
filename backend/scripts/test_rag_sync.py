"""RAG 知识库更新闭环测试：增 / 改 / 删 / 幂等 / 软删除。

覆盖「RAG 知识库更新应对方案」的核心承诺：
- 新增：文档入库后可检索
- 修改：定点修正后旧内容零残留（按 doc_id 删旧 + 重建）
- 删除：源文件消失后不再召回
- 幂等：连续两次 sync 第二次全跳过（零重复嵌入）
- 软删除：status=inactive / 过期文档检索不到，但台账可查（审计）

使用独立临时目录（kb_root / chroma 目录），不污染真实知识库。
"""
import shutil
import tempfile
from pathlib import Path

from app.rag.loader import scan_docs
from app.rag.retriever import retrieve
from app.rag.store import ChromaStore, create_store
from app.rag.sync import run_sync

TMP = Path(tempfile.mkdtemp(prefix="kb_test_"))
KB_ROOT = TMP / "docs"
CHROMA_DIR = TMP / "chroma"

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

DOC_A_V2 = """---
scenario: vpn
doc_title: 测试文档 A（v2 修正）
error_codes: ["800"]
risk: low
action: vpn.renew_certificate
version: "2.0"
status: active
valid_to: "2099-12-31"
source_url: docs/knowledge/vpn/test-a.md
---

# 测试文档 A

## 症状

VPN 报错 800 且证书过期。

## 处理

执行证书续期操作，并通知用户重新拨号验证。（v2 新增步骤）
"""

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

DOC_B_INACTIVE = DOC_B.replace('status: active', 'status: inactive')


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_store() -> ChromaStore:
    return ChromaStore(persist_dir=str(CHROMA_DIR), collection_name="kb_test")


def clean_ledger(kb_name: str) -> None:
    """清理指定 kb_name 的台账残留（测试开始/结束都清，保证可重复运行）。"""
    from app.db import SessionLocal
    from app.models import KbDocument
    with SessionLocal() as db:
        db.query(KbDocument).filter_by(kb_name=kb_name).delete()
        db.commit()


def main() -> None:
    print(f"=== 测试临时目录: {TMP} ===\n")
    clean_ledger("test")  # 上次运行若中断，先清残留

    # ---- 用例 1：新增 ----
    write(KB_ROOT / "vpn" / "test-a.md", DOC_A)
    write(KB_ROOT / "email" / "test-b.md", DOC_B)
    store = make_store()
    r1 = run_sync(KB_ROOT, store, kb_name="test")
    assert len(r1.added) == 2 and not r1.failed, f"新增失败: {r1.summary}"
    print(f"✓ 用例1 新增：{r1.summary}（库内 {store.count()} chunk）")

    hits = retrieve("证书过期 800 续期", scenario="vpn", top_k=3, store=store)
    # 混合检索下 top1 可能是症状章节（含更多关键词）——断言"命中正确文档"而非特定章节
    assert hits and any(h.metadata.get("source_url") == "docs/knowledge/vpn/test-a.md"
                        for h in hits), "新增文档检索不到"
    print(f"✓ 用例1 新增可检索：命中 {hits[0].metadata.get('source_url')}")

    # ---- 用例 2：修改（定点修正） ----
    write(KB_ROOT / "vpn" / "test-a.md", DOC_A_V2)  # 内容变化（version 1.0 → 2.0）
    r2 = run_sync(KB_ROOT, store, kb_name="test")
    assert len(r2.updated) == 1 and not r2.failed, f"更新失败: {r2.summary}"
    assert store.count() == 6, f"更新后 chunk 数应不变（6），实际 {store.count()}"
    print(f"✓ 用例2 修改：{r2.summary}（chunk 数不变 = 删旧增新无残留）")

    hits = retrieve("证书过期 800 续期", scenario="vpn", top_k=3, store=store)
    new_texts = [h.text for h in hits if "重新拨号验证" in h.text]
    assert new_texts, "新内容未被检索到"
    # 旧内容零残留：v1 的 chunk（version=1.0）必须全部删除（按 doc_id 删旧 + 重建）
    old_chunks = [h for h in hits if h.metadata.get("version") == "1.0"]
    assert not old_chunks, f"v1 旧 chunk 仍有残留: {old_chunks[0].id}"
    print(f"✓ 用例2 修改后旧内容零残留（v2『重新拨号验证』可检索，无 version=1.0 chunk）")

    # ---- 用例 3：幂等（第二次全跳过） ----
    r3 = run_sync(KB_ROOT, store, kb_name="test")
    assert len(r3.skipped) == 2 and not r3.added and not r3.updated, f"幂等失败: {r3.summary}"
    print(f"✓ 用例3 幂等：{r3.summary}（零重复嵌入）")

    # ---- 用例 4：软删除（status=inactive） ----
    write(KB_ROOT / "email" / "test-b.md", DOC_B_INACTIVE)
    r4 = run_sync(KB_ROOT, store, kb_name="test")
    assert len(r4.updated) == 1, f"软删除应走 updated: {r4.summary}"
    hits = retrieve("邮箱无法收发", scenario="email", top_k=3, store=store)
    assert not hits, "inactive 文档不应被检索到"
    from app.db import SessionLocal
    from app.models import KbDocument
    with SessionLocal() as db:
        row = db.query(KbDocument).filter_by(kb_name="test", path="email/test-b.md").first()
        assert row and row.status == "inactive", "台账应保留 inactive 记录（审计）"
    print(f"✓ 用例4 软删除：检索不到但台账保留 status=inactive（审计可查）")

    # ---- 用例 5：物理删除（源文件消失） ----
    (KB_ROOT / "email" / "test-b.md").unlink()
    r5 = run_sync(KB_ROOT, store, kb_name="test")
    assert len(r5.deleted) == 1, f"物理删除失败: {r5.summary}"
    hits = retrieve("邮箱无法收发", scenario="email", top_k=3, store=store)
    assert not hits, "已删除文档不应再被召回"
    with SessionLocal() as db:
        row = db.query(KbDocument).filter_by(kb_name="test", path="email/test-b.md").first()
        assert row and row.status == "removed", "台账应置 removed"
    print(f"✓ 用例5 物理删除：不再召回，台账置 removed（审计痕迹保留）")

    # ---- 用例 6：删除后可新增（removed → active 复活） ----
    write(KB_ROOT / "email" / "test-b.md", DOC_B)
    r6 = run_sync(KB_ROOT, store, kb_name="test")
    assert len(r6.added) == 1, f"复活失败: {r6.summary}"
    hits = retrieve("邮箱无法收发", scenario="email", top_k=1, store=store)
    assert hits, "复活后应可检索"
    print(f"✓ 用例6 复活：removed → active，可重新检索")

    print(f"\n全部用例通过 🎉  库内最终 {store.count()} chunk")
    clean_ledger("test")
    shutil.rmtree(TMP)


if __name__ == "__main__":
    main()

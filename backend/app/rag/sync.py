"""reconcile 引擎：让向量库与源文档保持一致（知识库生命周期管理）。

流程（幂等 + 失败自愈）：
1. 扫描 docs/knowledge/**/*.md → 每篇算 doc_hash（内容 + 元数据 sha256）
2. 读 MySQL 台账 kb_documents（path → doc_id / doc_hash / status）
3. 逐篇对比：
   - path 不在台账 → 【新增】建台账行 → 切分 → 嵌入 → 入库
   - doc_hash 变了  → 【修改】重切重嵌 → 覆盖入库 → 清理多余旧块
   - 台账有但文件消失 → 【删除】删 chunk + 台账置 removed（保留审计痕迹）
   - 未变            → 【跳过】（幂等核心：连续跑两次，第二次全跳过）
4. 失败自愈：单文档任一步异常 → 该文档整体不提交、台账 hash 不更新，
   下次 sync 自动重试；候选构建失败不可发布。

写入策略：
- 先完成嵌入并 upsert、核对全部预期 ID，再清理多余旧块。
- 嵌入失败不删除旧知识；Chroma 不支持跨 SQL/向量库事务，upsert 部分失败
  仍可能留中间态，靠幂等重试修复。线上使用候选构建及发布门禁隔离中间态。

语义去重（轻量版）：新增文档时，抽样首个 chunk 去库中检索，
相似度 ≥ 0.95 且来自其他文档 → 报告标 duplicate（人工确认），不阻止入库。
完整向量语义去重与混合检索一起在 Phase 2 强化。
"""
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.models import KbDocument
from app.rag.embedder import embed_texts
from app.rag.loader import KnowledgeDoc, scan_docs
from app.rag.splitter import split_all
from app.rag.store import VectorStore, create_store

# 语义去重阈值：与库中已有 chunk 的相似度达到此值视为重复
DUP_THRESHOLD = 0.95
# 台账分批 commit 间隔（防长事务锁表；见 run_sync 事务策略说明）
COMMIT_EVERY = 50


@dataclass
class SyncReport:
    """一次同步的完整报告（写 ChangeLog / 前端展示用）。"""
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, error)

    @property
    def summary(self) -> str:
        return (f"新增 {len(self.added)} / 更新 {len(self.updated)} / "
                f"删除 {len(self.deleted)} / 跳过 {len(self.skipped)} / "
                f"疑似重复 {len(self.duplicates)} / 失败 {len(self.failed)}")


def _doc_hash(doc: KnowledgeDoc) -> str:
    """内容指纹：正文 + 元数据（排序保证稳定），任一变化都触发重建。"""
    meta_json = json.dumps(doc.meta, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{meta_json}\n{doc.content}".encode()).hexdigest()


def _inject_hash(chunks, doc_hash: str):
    """sync 才知道整篇的 doc_hash（splitter 切块时还不知），写入每个 chunk。"""
    for c in chunks:
        c.metadata["doc_hash"] = doc_hash
    return chunks


def _chunks_match(store, chunks, doc_hash):
    got = store.get_chunks([chunk.id for chunk in chunks])
    return all(chunk.id in got and got[chunk.id]["text"] == chunk.text
               and got[chunk.id]["metadata"].get("doc_hash") == doc_hash for chunk in chunks)


def _sync_doc(doc: KnowledgeDoc, doc_hash: str, store: VectorStore,
              report: SyncReport) -> None:
    """新增或更新一篇文档（切分 → 嵌入 → 入库）。任一异常向上抛（由调用方兜底）。"""
    split = split_all([doc])[0]
    chunks = _inject_hash([split.parent, *split.children], doc_hash)

    # 语义去重（轻量版）：抽样首个子块的向量，去库中查最相似的一条
    if split.children:
        first_vector = embed_texts([split.children[0].text])[0]
        similar = store.query(first_vector, top_k=1,
                              where={"scenario": doc.meta.get("scenario")})
        if similar and similar[0].score >= DUP_THRESHOLD \
                and similar[0].metadata.get("doc_id") != doc.doc_id:
            report.duplicates.append(f"{doc.rel_path} 疑似与 "
                                     f"{similar[0].metadata.get('source_url')} 重复 "
                                     f"(score={similar[0].score})")

    old_ids = set(store.get_doc_ids(doc.doc_id))

    # 嵌入（子块+父块一起，一次批量调用；文档多时分批由 embed_texts 处理）
    texts = [c.text for c in chunks]
    vectors = embed_texts(texts)
    if len(vectors) != len(chunks):
        raise ValueError("嵌入数量与分块数量不一致")
    store.upsert_chunks(chunks, vectors)
    expected = {c.id for c in chunks}
    if not _chunks_match(store, chunks, doc_hash):
        raise RuntimeError("向量写入不完整，保留旧块并等待重试")
    obsolete = old_ids - expected
    if obsolete:
        store.delete_ids(sorted(obsolete))


def run_sync(kb_root: Path | None = None, store: VectorStore | None = None,
             kb_name: str = "default") -> SyncReport:
    """执行一次同步：扫描源文档 → 与台账对比 → 增/改/删/跳过。

    kb_name：台账隔离键（默认 default；测试/多知识库用独立名，
    避免测试误把真实知识库的文档判为"源文件消失"而删除）。

    事务策略（为什么分批 commit + savepoint）：
    - 单篇失败用 savepoint 回滚（只回滚这一篇，不连坐同批次的成功文档）
    - 每 COMMIT_EVERY 篇 commit 一次：避免全量长事务持有表锁，
      阻塞其他进程的 DDL/查询（实测：9000 篇不 commit 会卡死 ALTER TABLE）
    """
    kb_root = kb_root or Path(settings.KB_ROOT)
    store = store or create_store()
    report = SyncReport()

    scanned = scan_docs(kb_root)
    docs_by_path = {d.rel_path: d for d in scanned.docs}
    report.failed.extend((path, "源文件解析或转译失败，保留旧知识") for path in scanned.skipped)

    # 读台账（只读当前 kb_name 的记录）
    with SessionLocal() as db:
        ledger = {r.path: r for r in db.query(KbDocument)
                  .filter_by(kb_name=kb_name).all()}

        for i, (rel_path, doc) in enumerate(docs_by_path.items()):
            doc_hash = _doc_hash(doc)
            existing = ledger.get(rel_path)
            sp = db.begin_nested()  # savepoint：单篇失败只回滚这一篇
            try:
                if existing is None:
                    # ---- 新增 ----
                    row = KbDocument(kb_name=kb_name, path=rel_path, doc_id=doc.doc_id,
                                     doc_hash=doc_hash, status="active")
                    db.add(row)
                    db.flush()  # 拿到自增 id（虽然当前不用，保持台账语义完整）
                    _sync_doc(doc, doc_hash, store, report)
                    target = row
                    report.added.append(rel_path)
                elif existing.status == "removed":
                    # 曾经删除过又出现 → 当作新增（台账已留审计痕迹）
                    existing.status = "active"
                    existing.doc_hash = doc_hash
                    _sync_doc(doc, doc_hash, store, report)
                    target = existing
                    report.added.append(rel_path)
                elif existing.doc_hash != doc_hash:
                    # ---- 修改：新块成功入库后清理多余旧块 ----
                    _sync_doc(doc, doc_hash, store, report)
                    existing.doc_hash = doc_hash
                    target = existing
                    report.updated.append(rel_path)
                else:
                    # ---- 未变：跳过（幂等） ----
                    # 幂等补写（蓝绿候选模式的关键）：台账说"在库"，但【目标库】
                    # 可能没有这篇（候选库 clear 后全量重建时，所有文档都会走这里）。
                    # 只对"目标库缺这篇"才补写——正常增量模式库里有 → 零成本跳过。
                    split = split_all([doc])[0]
                    expected_ids = {c.id for c in [split.parent, *split.children]}
                    chunks = [split.parent, *split.children]
                    if set(store.get_doc_ids(doc.doc_id)) != expected_ids or not _chunks_match(store, chunks, doc_hash):
                        _sync_doc(doc, doc_hash, store, report)
                    # 轻量回填：老库可能没存 valid_to（迁移前入库的文档），
                    # 这里只补台账列、不重建 chunk（hash 没变 = 内容没变）
                    if existing.valid_to is None and doc.meta.get("valid_to"):
                        existing.valid_to = doc.meta["valid_to"]
                        sp.commit()  # 有写操作：必须 commit 才生效（savepoint 内）
                    else:
                        # 无写操作：释放 savepoint（回滚无害）
                        # 踩坑：不能无条件 rollback——会把上面的回填一起回滚掉
                        sp.rollback()
                    report.skipped.append(rel_path)
                    continue
                # 更新台账 chunk_count（本文档的 chunk 数：父块 + 子块）
                split = split_all([doc])[0]
                target.chunk_count = 1 + len(split.children)
                # 同步 frontmatter 的 status（软删除：文档置 inactive → 台账也置 inactive）
                target.status = doc.meta.get("status", "active")
                # 有效期冗余快照（过期预警只查台账，不碰源文件/向量库）
                target.valid_to = doc.meta.get("valid_to")
                target.changelog = f"sync {doc_hash[:8]}"
                sp.commit()  # 释放 savepoint
            except Exception as e:
                sp.rollback()  # 只回滚这一篇的 SQL（Chroma 写入不可回滚，靠幂等覆盖）
                report.failed.append((rel_path, str(e)))
            # 分批 commit：防长事务锁表（全量补入 4.7 万条时尤其关键）
            if (i + 1) % COMMIT_EVERY == 0:
                db.commit()

        # ---- 台账有但源文件已消失（仅当前 kb_name）：删除 ----
        # 条件用 status != "removed"：active（物理删除）和 inactive（软删后文件最终消失）
        # 都应置 removed；只有已 removed 的跳过（幂等）
        for path, row in list(ledger.items()):
            if path not in docs_by_path and not (kb_root / path).exists() and row.status != "removed" and row.kb_name == kb_name:
                delete_sp = db.begin_nested()
                try:
                    store.delete_by_doc_id(row.doc_id)
                    row.status = "removed"
                    row.changelog = "source file removed"
                    delete_sp.commit()
                    report.deleted.append(path)
                except Exception as e:
                    delete_sp.rollback()
                    report.failed.append((path, f"delete: {e}"))

        db.commit()

    # BM25 是进程内缓存，不能只靠 chunk 数量判断失效：文档修改/软删除
    # 可能保持 chunk 数不变，但正文或 status metadata 已经变化。同步是所有
    # 知识库写入的统一边界，变更完成后主动淘汰对应 collection 的缓存，
    # 下一次检索再懒加载最新内容。
    if report.added or report.updated or report.deleted:
        collection_name = getattr(store, "collection_name", None)
        if collection_name:
            from app.rag import retriever

            retriever._BM25_CACHE.pop(collection_name, None)

    return report





@contextmanager
def publication_lock():
    """同主机进程间互斥，覆盖候选构建及指针切换。"""
    import fcntl
    lock_path = Path(settings.KB_VECTOR_DIR) / ".publication.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("知识库正在同步或切换，请稍后重试") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def build_candidate(target, kb_root=None, kb_name="default"):
    """在持有 publication_lock 时调用；失败构建不可发布，标记随 Chroma 持久化。"""
    if target.collection_name.startswith("kb_gen_"):
        raise ValueError("不可用旧全量同步覆盖版本化索引")
    target.set_build_ready(False)
    from app.rag import retriever

    retriever._BM25_CACHE.pop(target.collection_name, None)
    target.clear()
    target.set_build_ready(False)
    report = run_sync(kb_root, store=target, kb_name=kb_name)
    target.set_build_ready(not report.failed and target.count() > 0)
    return report

def main() -> None:
    """CLI 入口：python -m app.rag.sync [--kb-root path] [--kb-name name]

    蓝绿流程（Phase 5）：sync 永远全量写入【候选 collection】（先清空再重建），
    在岗 collection 不受影响；跑完报告后手动调用切换接口生效。
    """
    import argparse

    from app.db import init_db

    parser = argparse.ArgumentParser(description="同步知识库（源文档 → 向量库）")
    parser.add_argument("--kb-root", default=None, help="源文档目录（默认用配置 KB_ROOT）")
    parser.add_argument("--kb-name", default="default",
                        help="台账实例名（规模压测用 scale，与运维知识库隔离）")
    parser.add_argument("--collection", default=None,
                        help="目标 collection（默认蓝绿候选库；独立语料库如 scale 用"
                             "独立 collection——否则会被蓝绿交替清掉，踩过坑）")
    args = parser.parse_args()

    init_db()  # 确保 kb_documents 台账表存在（幂等）
    # 全量替换语义：先清空目标库再写入（失败则目标为空，active 不受影响）
    with publication_lock():
        target = create_store(collection_name=args.collection or settings.candidate_collection)
        report = build_candidate(target, Path(args.kb_root) if args.kb_root else None,
                                 kb_name=args.kb_name)
    print(f"=== 知识库同步报告（kb_name={args.kb_name}）===")
    print(report.summary)
    for path in report.added:
        print(f"  [新增] {path}")
    for path in report.updated:
        print(f"  [更新] {path}")
    for path in report.deleted:
        print(f"  [删除] {path}")
    for path, err in report.failed:
        print(f"  [失败] {path}: {err}")
    for dup in report.duplicates:
        print(f"  [疑似重复] {dup}")
    # 蓝绿提示：写入的是候选库，验证后再切换（零中断发布）
    if args.collection:
        print(f"\n已写入独立 collection（{args.collection}）——不参与蓝绿交替，"
              f"不会被后续 sync 清掉。")
    else:
        print(f"\n已写入候选库（{settings.candidate_collection}），在岗仍是"
              f"（{settings.active_collection}）。")
        print("验证候选库后执行切换：python -m scripts.test_shadow（影子对比）"
              "→ POST /api/kb/switch（或调 kb.py 的 switch_active）")


if __name__ == "__main__":
    main()

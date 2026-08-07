"""知识库管理 API：台账 / 统计 / 同步（蓝绿）/ 检索调试 / 切换。

核心设计（面试可讲）：
- 台账（kb_documents 表）是知识库的"事实账本"：列表、预警、ChangeLog
  全查 MySQL——读源文件要解析/调模型（重），读台账是索引查询（快）。
- 蓝绿发布：sync 全量写入【候选 collection】（线上零中断）→ 验证 →
  切指针（秒级生效 + 秒级回滚）；切换只改一个落盘文件，无数据搬迁。
"""
import time
from contextlib import suppress
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.conversations import get_db
from app.config import settings
from app.models import KbDocument, User
from app.rag.retriever import RetrievalStats, retrieve
from app.rag.store import Hit, create_store
from app.rag.sync import run_sync
from app.security import require_admin
from app.services.audit_service import audit

router = APIRouter(prefix="/api/kb", tags=["kb"])

# 过期预警阈值：valid_to 距今 <= 此天数 → 标预警（前端黄/红提示）
EXPIRING_DAYS = 30


class KbDebugRequest(BaseModel):
    query: str
    scenario: str | None = None  # 过滤场景（如 vpn）；不传 = 全部
    top_k: int = 5


def _hit_out(hit: Hit, max_text: int = 140) -> dict:
    """Hit → 前端可展示的字典（截断文本防刷屏）。"""
    meta = hit.metadata or {}
    return {
        "id": hit.id,
        "score": hit.score,
        "source_url": meta.get("source_url") or meta.get("doc_id", "?"),
        "chunk_type": meta.get("chunk_type", "child"),
        "scenario": meta.get("scenario", ""),
        "text": hit.text[:max_text],
    }


@router.get("/documents")
def list_documents(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    status: str | None = None,
    kb_name: str = "default",
):
    """台账列表（文档体检报告）：状态 / 块数 / 有效期 / 变更记录。

    默认只列 default 实例（运维知识库）；压测库（scale）用 kb_name 参数显式查。
    expiring_days：距过期剩余天数（valid_to 距今 <= 30 天预警；负数=已过期；
    None=未设有效期）。预警判断放后端（日期口径统一），前端只负责展示。
    """
    q = db.query(KbDocument).filter_by(kb_name=kb_name)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(KbDocument.path).all()

    today = datetime.now().date()
    docs = []
    for r in rows:
        expiring_days = None
        if r.valid_to:
            with suppress(ValueError):
                # 格式异常（如 20991231）：不预警，保持原始字符串展示
                expiring_days = (datetime.strptime(r.valid_to, "%Y-%m-%d").date()
                                 - today).days
        docs.append({
            "id": r.id,
            "path": r.path,
            "status": r.status,
            "chunk_count": r.chunk_count,
            "valid_to": r.valid_to,
            "expiring_days": expiring_days,
            "changelog": r.changelog,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        })
    return {"docs": docs, "total": len(docs)}


@router.get("/stats")
def kb_stats(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """库统计 + 蓝绿状态（管理页顶部卡片 / 切换按钮的状态依据）。"""
    active = create_store()               # 在岗库
    candidate = create_store(collection_name=settings.candidate_collection)
    # 统计口径按实例隔离：doc_count 是运维知识库（default）的文档数；
    # total_docs 含压测库（scale）——列表/预警都只看 default，避免 3300 条干扰
    active_rows = (db.query(KbDocument)
                   .filter_by(status="active", kb_name="default").all())
    return {
        "active_collection": settings.active_collection,
        "candidate_collection": settings.candidate_collection,
        "active_chunks": active.count(),
        "candidate_chunks": candidate.count(),
        "doc_count": len(active_rows),          # default 实例在册文档数
        "total_docs": db.query(KbDocument).filter_by(status="active").count(),
        "removed_docs": db.query(KbDocument).filter_by(status="removed",
                                                      kb_name="default").count(),
        "last_sync": max((r.updated_at for r in active_rows), default=None),
    }


@router.post("/sync")
def kb_sync(
    request: Request,
    user: User = Depends(require_admin),
):
    """触发一次同步（蓝绿：全量写入候选库，在岗库不动）。

    流程：清空候选 → reconcile（增/改/删/跳过）→ 返回报告。
    同步完成后【不自动切换】——先看报告/跑影子测试，再手动切换。
    """
    t0 = time.perf_counter()
    # 全量替换语义：候选库清空重建（失败则候选为空，active 零影响，
    # 下次 sync 自动重试——蓝绿的核心：发布失败不影响线上）
    candidate = create_store(collection_name=settings.candidate_collection)
    candidate.clear()
    try:
        report = run_sync(store=candidate, kb_name="default")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"同步失败：{e}") from e
    elapsed = round((time.perf_counter() - t0) * 1000)
    from app.db import SessionLocal  # 审计需要 db 会话（本地开一个，不污染接口依赖）
    with SessionLocal() as db:
        audit(db, user, "kb_sync", {
            "added": len(report.added), "updated": len(report.updated),
            "deleted": len(report.deleted), "failed": len(report.failed),
            "elapsed_ms": elapsed,
        }, request=request)
    return {
        "summary": report.summary,
        "added": report.added,
        "updated": report.updated,
        "deleted": report.deleted,
        "skipped_count": len(report.skipped),
        "duplicates": report.duplicates,
        "failed": report.failed,
        "elapsed_ms": elapsed,
        # 蓝绿提示：告诉用户现在在岗/候选的状态，引导下一步切换
        "active_collection": settings.active_collection,
        "candidate_collection": settings.candidate_collection,
        "candidate_chunks": create_store(
            collection_name=settings.candidate_collection).count(),
    }


@router.post("/switch")
def kb_switch(
    request: Request,
    user: User = Depends(require_admin),
):
    """蓝绿切换：active 指针 → 候选库（回滚 = 再调一次，两库交替）。

    安全校验：候选库为空禁止切换（切到空库 = 全站检索瘫痪）。
    切换只写一个落盘文件（.rag/kb_active.txt）——秒级生效、重启保持。
    """
    candidate = create_store(collection_name=settings.candidate_collection)
    if candidate.count() == 0:
        raise HTTPException(status_code=409, detail="候选库为空，禁止切换（请先同步）")
    new_active = settings.switch_active()
    from app.db import SessionLocal
    with SessionLocal() as db:
        audit(db, user, "kb_switch", {"switched_to": new_active}, request=request)
    return {
        "switched_to": new_active,
        "active_chunks": create_store().count(),
        "candidate_chunks": create_store(
            collection_name=settings.candidate_collection).count(),
        "note": "回滚：再次调用本接口即可切回旧库（旧库未被覆盖前有效）",
    }


@router.post("/debug")
def kb_debug(
    body: KbDebugRequest,
    user: User = Depends(require_admin),
):
    """检索调试：跑一次完整混合检索，返回双路召回 + 融合的每一步明细。

    用途（知识库管理页"检索调试"）：运营者看到"为什么这个 query 没命中"，
    是向量路没召回？BM25 没召回？还是都被 RRF 挤掉了。
    """
    t0 = time.perf_counter()
    stats = RetrievalStats()
    hits = retrieve(body.query, body.scenario, body.top_k, stats=stats, debug=True)
    elapsed = round((time.perf_counter() - t0) * 1000)
    return {
        "query": body.query,
        "scenario": body.scenario,
        "elapsed_ms": elapsed,
        "stats": {
            "vector_recall": stats.vector_recall,
            "bm25_recall": stats.bm25_recall,
            "fused_total": stats.fused_total,
            "reranked": stats.reranked,
        },
        "vector_hits": [_hit_out(h) for h in (stats.debug_vector_hits or [])],
        "bm25_hits": [_hit_out(h) for h in (stats.debug_bm25_hits or [])],
        "final": [_hit_out(h) for h in hits],
    }

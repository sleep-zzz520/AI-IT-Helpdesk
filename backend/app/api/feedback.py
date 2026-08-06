"""反馈闭环 API：用户 👍/👎 反馈落库 + 后台负反馈分析（文档缺失 vs 过时）。

负反馈分析的核心思路（为什么可以零成本归类）：
- 每轮问答的 Trace 里已经记录了 rag_query 节点的 sources（命中了哪些文档）
- 用户点 👎 = "这条回答没帮到我"，此时看 sources：
    sources 为空 → 知识库里根本没有相关内容 → 【文档缺失】（应补文档）
    sources 非空 → 有文档但回答没用 → 【文档过时/不准】（应修文档）
- 不需要再调模型分析——已有的可观测性数据直接反哺运营。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.conversations import get_db
from app.models import Conversation, Message, User
from app.schemas import FeedbackCreate
from app.security import get_current_user
from app.services.audit_service import audit

router = APIRouter(tags=["feedback"])


@router.post("/api/messages/{msg_id}/feedback")
def submit_feedback(
    msg_id: int,
    body: FeedbackCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """提交/修改/取消 👍/👎 反馈。幂等覆盖：重复提交覆盖旧值。

    - feedback=up/down：设置（改主意换边 = 直接发新值，覆盖）
    - feedback=null：取消（点错时撤回，不留错误数据）
    """
    msg = db.get(Message, msg_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="消息不存在")
    # 多租户：只能给自己的会话消息反馈
    if msg.conversation.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="消息不存在")
    if msg.role != "assistant":
        raise HTTPException(status_code=400, detail="只对 Agent 回复反馈")
    msg.feedback = body.feedback
    db.commit()
    audit(db, user, "feedback", {"msg_id": msg_id, "feedback": body.feedback},
          request=request)
    return {"id": msg.id, "feedback": msg.feedback}


def _classify_down(msg: Message) -> dict:
    """给一条 👎 消息归类：missing（文档缺失）/ stale（文档过时）/ other。

    规则（见模块 docstring）：
    - 消息之前最近的 rag_query trace 无 sources → missing
    - 有 sources → stale（命中但用户不满意：内容过时/答非所问）
    - 没有 rag_query trace → other（执行失败/转人工等非问答场景）
    """
    # 该消息时间点之前最近的 rag_query trace（消息是"对哪轮回答的反馈"）
    rq = None
    for t in msg.conversation.traces:
        if t.node == "rag_query" and (t.created_at or msg.created_at) <= msg.created_at:
            rq = t
    if rq is None:
        return {"category": "other", "reason": "非知识问答场景（执行/转人工等）",
                "query": "", "sources": []}
    result = rq.result or {}
    sources = list(result.get("sources") or [])
    if not sources:
        return {"category": "missing", "reason": "无命中文档 → 知识库缺失，应补文档",
                "query": result.get("query", ""), "sources": []}
    return {"category": "stale", "reason": "有命中文档但用户不满意 → 文档过时/不准，应修文档",
            "query": result.get("query", ""), "sources": sources}


@router.get("/api/feedback/analysis")
def feedback_analysis(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """后台负反馈分析：统计 + 逐条归类（知识库运营的数据源，仅登录用户）。

    演示数据量小，直接在内存统计；生产量级应改为 SQL 聚合。
    多租户：只统计当前租户的消息（跨租户的反馈不混进来）。
    """
    rows = (db.query(Message)
            .join(Message.conversation)
            .filter(Conversation.tenant_id == user.tenant_id)
            .all())
    ups = [m for m in rows if m.feedback == "up"]
    downs = [m for m in rows if m.feedback == "down"]
    rated = ups + downs

    items = []
    for m in downs:
        cls = _classify_down(m)
        items.append({
            "message_id": m.id,
            "conversation_id": m.conversation_id,
            "content": m.content[:200],          # 截断：明细展示足够，省带宽
            "created_at": m.created_at.isoformat() if m.created_at else None,
            **cls,
        })

    by_category: dict[str, int] = {}
    for it in items:
        by_category[it["category"]] = by_category.get(it["category"], 0) + 1

    return {
        "summary": {
            "total": len(rows),
            "up": len(ups),
            "down": len(downs),
            "unrated": len(rows) - len(rated),
            # 差评率：有反馈消息里的 👎 占比（回答质量口径）
            "down_rate": round(len(downs) / len(rated), 3) if rated else 0,
        },
        "by_category": by_category,
        "items": items,
    }

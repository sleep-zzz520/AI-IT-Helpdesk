"""会话相关 API 路由：创建会话 / 发消息（SSE 流式）/ 查 Trace。

【多租户隔离 + 权限】——本文件是权限体系的重点落点：
- 所有接口要求登录（get_current_user 依赖）
- 创建会话：user_id / tenant_id 从登录用户注入（不信任前端传值）
- 查会话/发消息/查 trace：先校验租户，跨租户 → 404（"不存在"而非"无权限"，
  避免泄露别租户的会话存在性）
"""
import json
import logging
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.graph import graph
from app.config import settings
from app.db import SessionLocal
from app.models import Conversation, User
from app.schemas import (
    ConversationCreate,
    ConversationOut,
    MessageCreate,
    MessageOut,
    StatusLogOut,
    TraceOut,
)
from app.security import get_current_user
from app.services import session_service
from app.services.audit_service import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def get_db():
    """依赖注入：每个请求开一个数据库会话，用完自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_agent_stream(state: dict):
    """跑一轮 Agent 并【边跑边产出】。

    生成器 yield 两种事件：
    - ("node", new_traces)：每完成一个节点，产出该节点新增的 trace（含耗时）
    - ("done", final_state, total_ms)：全部跑完，产出最终 state 和本轮总耗时

    为什么用 stream 而不是 invoke：invoke 只返回最终 state，拿不到【每个节点各自的
    耗时】；stream(updates) 在节点执行完的瞬间 yield，此刻与上一节点的时间差 ≈ 节点耗时。
    合并逻辑与 invoke 等价：trace 用 reducer 累加，其余字段直接覆盖
    （所有节点返回的都是完整值，如 messages = 历史 + 新增）。
    """
    final = dict(state)
    final["trace"] = list(state.get("trace", []))  # 先复制历史 trace，再累加新增
    t_all = time.perf_counter()
    prev_t = t_all
    for chunk in graph.stream(state, stream_mode="updates"):
        for update in chunk.values():
            elapsed_ms = round((time.perf_counter() - prev_t) * 1000)
            # 给该节点新增的 trace 补耗时；parallel 内部已注入精确耗时，不覆盖
            for t in update.get("trace", []):
                t.setdefault("elapsed_ms", elapsed_ms)
            new_traces = update.get("trace", [])
            final["trace"] += new_traces
            for k, v in update.items():
                if k != "trace":
                    final[k] = v
            prev_t = time.perf_counter()
            yield "node", new_traces
    total_ms = round((time.perf_counter() - t_all) * 1000)
    yield "done", final, total_ms


def _get_owned_conversation(db: Session, conv_id: int, user: User) -> Conversation:
    """按用户租户获取会话（跨租户 → 404，不泄露存在性）。

    多租户隔离核心：所有按 id 查会话的地方都必须过这道校验。
    """
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv


@router.post("", response_model=ConversationOut)
def create_conversation(
    body: ConversationCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """开新工单/会话。身份从登录态注入（不信任前端传的 user_id）。"""
    # 身份注入：user_id 用登录用户名，tenant_id 用登录用户租户
    conv = session_service.create_conversation(
        db, user_id=user.username, tenant_id=user.tenant_id)
    audit(db, user, "create_conversation",
          {"conv_id": conv.id, "user_id": conv.user_id}, request=request)
    return conv


@router.get("/{conv_id}", response_model=ConversationOut)
def get_conversation(
    conv_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查会话（工单状态/intent/ticket_id，前端刷新用）。"""
    return _get_owned_conversation(db, conv_id, user)


@router.post("/{conv_id}/messages")
def send_message(
    conv_id: int,
    body: MessageCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """发消息（SSE 流式）：边跑 Agent 边推送节点进度，最后推完整结果。

    事件流：
      event: node  data: {"traces": [...]}   每完成一个节点推一次（执行链路实时跳动）
      event: done  data: {messages, elapsed_ms, ...}   全部完成（含存库后的最终数据）
    """
    # 0. 租户校验：不能给别的租户的会话发消息
    _get_owned_conversation(db, conv_id, user)
    audit(db, user, "send_message", {"conv_id": conv_id}, request=request)
    # 1. 从数据库恢复会话记忆
    state = session_service.load_state(db, conv_id)
    # 写操作身份上下文只能由已认证的 API 入口填写，绝不从前端 body 或模型抽取。
    # operation_id 会一路传到下游监控服务；当前请求内的重放会复用它。
    state.update({
        "actor_id": user.username,
        "tenant_id": user.tenant_id,
        "execution_source": "web_agent",
        "operation_id": f"web-agent:{uuid4()}",
    })
    # 2. 把新消息追加进历史（可选带截图 base64，OCR 用）
    msg = {"role": "user", "content": body.content}
    if body.image:
        msg["image"] = body.image
    state["messages"] = [*state.get("messages", []), msg]
    # 2.3 本轮用户消息（工单状态机触发用：new→processing 与"未解决"重开）
    state["new_user_message"] = body.content
    # 2.5 模型链模式（前端"速度/准确"切换）→ 注入 state，节点据此选链
    state["model_chain"] = settings.GLM_MODELS_FAST if body.mode == "fast" else settings.GLM_MODELS

    def event_stream():
        """SSE 生成器：节点事件实时推，done 事件携带最终结果。

        落库策略：**边跑边存**（节点完成即增量落库）——客户端中途断开
        （刷新/断网）时生成器被 cancel，如果只在 done 落库会静默丢整轮数据；
        增量落库保证已完成的节点/消息都在库里，断开只丢"还没跑的节点"。
        save_turn 内部用长度对比只存新增，重复调用幂等。
        """
        acc_trace = list(state.get("trace", []))
        try:
            for event in _run_agent_stream(state):
                kind = event[0]
                if kind == "node":
                    _, new_traces = event
                    acc_trace += new_traces
                    # 增量落库：用户消息 + 已完成的节点 trace 立即持久化
                    conv = db.get(Conversation, conv_id)
                    session_service.save_turn(db, conv, {**state, "trace": acc_trace})
                    yield f"event: node\ndata: {json.dumps({'traces': new_traces}, ensure_ascii=False)}\n\n"
                else:
                    _, final, total_ms = event
                    # 全量落库（补 assistant 消息 + 总耗时 + 业务字段推进）
                    conv = db.get(Conversation, conv_id)
                    session_service.save_turn(db, conv, final, total_ms)
                    # 取最新 assistant 回复；消息带耗时（历史取已存的，本轮新增挂总耗时）
                    replies = [m for m in final["messages"] if m["role"] == "assistant"]
                    reply = replies[-1]["content"] if replies else "（无回复）"
                    n_before = len(state["messages"])

                    def to_out(i, m):
                        el = m.get("elapsed_ms")
                        if el is None and m["role"] == "assistant" and i >= n_before:
                            el = total_ms
                        # 带消息 id（👍/👎 反馈需要）：save_turn 已全量落库，
                        # conv.messages 与 final["messages"] 同序一一对应
                        mid = conv.messages[i].id if i < len(conv.messages) else None
                        return MessageOut(id=mid, role=m["role"],
                                          content=m["content"], elapsed_ms=el)

                    payload = {
                        "conversation_id": conv_id,
                        "reply": reply,
                        "messages": [to_out(i, m).model_dump() for i, m in enumerate(final["messages"])],
                        "traces": final["trace"],
                        "elapsed_ms": total_ms,
                        # 工单最新状态（save_turn 已推进），前端免二次请求
                        "conversation": {
                            "id": conv.id,
                            "user_id": conv.user_id,
                            "intent": conv.intent,
                            "status": conv.status,
                            "ticket_id": conv.ticket_id,
                        },
                    }
                    yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception as e:
            # 兜底：至少把用户消息落库（save_turn 幂等，长度对比不会重复），
            # 并推送明确 error 事件，前端能显示"执行失败"而不是"连接中断"
            conv = db.get(Conversation, conv_id)
            session_service.save_turn(db, conv, state)
            logger.error("Agent 执行异常: %s: %s", type(e).__name__, e)
            yield f"event: error\ndata: {json.dumps({'message': f'Agent 执行失败（{type(e).__name__}），您的消息已保存'}, ensure_ascii=False)}\n\n"
            return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 强制 nginx 关闭缓冲，否则 SSE 被攒着不推
        },
    )


@router.get("/{conv_id}/traces", response_model=list[TraceOut])
def get_traces(
    conv_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查会话的 Trace 记录（可视化面板数据源）。"""
    conv = _get_owned_conversation(db, conv_id, user)
    return conv.traces


@router.get("/{conv_id}/status_logs", response_model=list[StatusLogOut])
def get_status_logs(
    conv_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查工单状态流转历史（状态机履历：new→processing→resolved ...）。"""
    conv = _get_owned_conversation(db, conv_id, user)
    return [
        StatusLogOut(
            id=log.id, from_status=log.from_status, to_status=log.to_status,
            reason=log.reason,
            created_at=log.created_at.isoformat() if log.created_at else None,
        )
        for log in conv.status_logs
    ]

"""VPN 证书续期的受控执行入口。

这里是“真的调用外部系统”之前的最后一道业务边界。调用方不能只传一个
username；还必须提供受信任的调用主体、租户、风险决定、来源和 operation_id。
网页 Agent 现在使用它，未来 MCP 写 Tool 也必须使用它。
"""
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import AuditLog, ExecutionOperation, User
from app.services.execution_policy import AUTO_EXECUTION
from app.tools.monitor_api import renew_certificate

logger = logging.getLogger(__name__)

VPN_RENEW_ACTION = "vpn.renew_certificate"
ALLOWED_SOURCES = frozenset({"web_agent", "mcp"})


@dataclass(frozen=True)
class ExecutionRequest:
    """一次写操作所需的最小、可信上下文。

    ``operation_id`` 由入口在接收请求时生成并在重试时复用；模型不能自己
    编造它来替代身份验证。当前策略只允许用户为自己续期。
    """

    actor_id: str
    tenant_id: int | None
    target_user_id: str
    risk_decision: str
    source: str
    operation_id: str


def renew_vpn_certificate(request: ExecutionRequest) -> dict:
    """验证请求、建立幂等台账，再调用监控系统续期。"""
    # 运行时导入：测试会替换 app.db.SessionLocal 为内存 SQLite 工厂。
    from app import db as db_module

    with db_module.SessionLocal() as db:
        reason = _validate_request(db, request)
        if reason:
            return _deny(db, request, reason)

        existing = db.scalar(
            select(ExecutionOperation).where(
                ExecutionOperation.operation_id == request.operation_id
            )
        )
        if existing is not None:
            return _existing_result(existing, request)

        operation = ExecutionOperation(
            operation_id=request.operation_id,
            action=VPN_RENEW_ACTION,
            actor_id=request.actor_id,
            target_user_id=request.target_user_id,
            tenant_id=request.tenant_id,
            source=request.source,
            status="pending",
        )
        db.add(operation)
        try:
            # 先持久化 pending：进程在外部调用期间异常时，重试不会静默再发一次。
            db.commit()
        except IntegrityError:
            # 两个并发请求恰好同时查不到旧记录时，唯一索引仍是最后的护栏。
            db.rollback()
            existing = db.scalar(
                select(ExecutionOperation).where(
                    ExecutionOperation.operation_id == request.operation_id
                )
            )
            if existing is not None:
                return _existing_result(existing, request)
            raise

        try:
            # 把相同 operation_id 继续交给下游。若下游支持该幂等键，HTTP 重试也
            # 不会重复产生副作用；不支持时仍由本地台账挡住本服务层的重复调用。
            result = renew_certificate(request.target_user_id, request.operation_id)
        except Exception as exc:  # 监控适配层理论上会归一化异常，这里仍做最后兜底。
            logger.exception("VPN 续期调用出现未归一化异常: operation_id=%s", request.operation_id)
            result = {"status": "error", "reason": f"证书续期调用异常：{type(exc).__name__}"}

        final_result = {**result, "operation_id": request.operation_id}
        operation.status = "succeeded" if result.get("status") == "ok" else "failed"
        operation.result = final_result
        db.add(AuditLog(
            tenant_id=request.tenant_id,
            user_id=request.actor_id,
            action="execute_vpn_renew_certificate",
            detail={
                "operation_id": request.operation_id,
                "target_user_id": request.target_user_id,
                "source": request.source,
                "result_status": result.get("status"),
            },
        ))
        try:
            db.commit()
        except Exception:
            # ponytail: 外部系统成功但本地最终结果未落库时，保持 pending 并阻止自动
            # 重试；生产环境应补 provider 查询接口和 pending 操作的恢复任务。
            db.rollback()
            logger.exception("VPN 续期结果落库失败: operation_id=%s", request.operation_id)
            return {
                "status": "error",
                "reason": "续期结果未能持久化，已阻止自动重试，请转人工核验",
                "operation_id": request.operation_id,
            }
        return final_result


def _validate_request(db, request: ExecutionRequest) -> str | None:
    """返回拒绝原因；None 表示当前请求可进入副作用阶段。"""
    if not request.operation_id or len(request.operation_id) > 128:
        return "缺少合法的操作编号"
    if request.source not in ALLOWED_SOURCES:
        return "未知的执行来源"
    if request.risk_decision != AUTO_EXECUTION:
        return "当前风险策略不允许自动续期"
    if not request.actor_id or not request.target_user_id or not request.tenant_id:
        return "缺少可信的主体或租户信息"
    if request.actor_id != request.target_user_id:
        return "VPN 自助续期只能作用于当前登录用户"

    actor = db.scalar(
        select(User).where(
            User.username == request.actor_id,
            User.tenant_id == request.tenant_id,
            User.active == 1,
        )
    )
    if actor is None:
        return "当前主体不存在、已禁用或不属于该租户"
    return None


def _existing_result(existing: ExecutionOperation, request: ExecutionRequest) -> dict:
    """同一操作编号只能被原主体以原语义重放。"""
    if (
        existing.actor_id != request.actor_id
        or existing.tenant_id != request.tenant_id
        or existing.target_user_id != request.target_user_id
        or existing.action != VPN_RENEW_ACTION
    ):
        return {"status": "error", "reason": "操作编号不属于当前主体或目标"}
    if existing.status == "pending":
        return {
            "status": "error",
            "reason": "该操作仍在处理中，已阻止重复续期",
            "operation_id": existing.operation_id,
        }
    return existing.result or {
        "status": "error",
        "reason": "该操作缺少最终结果，请转人工核验",
        "operation_id": existing.operation_id,
    }


def _deny(db, request: ExecutionRequest, reason: str) -> dict:
    """拒绝也留痕；审计写入失败不改变“未执行”的事实。"""
    db.add(AuditLog(
        tenant_id=request.tenant_id,
        user_id=request.actor_id or "anonymous",
        action="deny_vpn_renew_certificate",
        detail={
            "operation_id": request.operation_id or None,
            "target_user_id": request.target_user_id or None,
            "source": request.source or None,
            "reason": reason,
        },
    ))
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("VPN 续期拒绝审计写入失败")
    return {"status": "error", "reason": reason, "operation_id": request.operation_id or None}

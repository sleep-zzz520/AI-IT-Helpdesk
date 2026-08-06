"""审计日志服务：给各 API 提供一行式写入接口。

用法（在 API 里）：
    audit(db, user, "send_message", {"conv_id": conv_id})
    audit(db, user, "kb_sync", {"added": 3}, request=request)  # 带 IP

原则：
- 只追加：审计日志永不修改/删除（合规与追溯的基本盘）
- 失败不影响主流程：写入审计失败只记 warning，不抛异常拖垮业务
"""
import logging

from app.models import AuditLog

logger = logging.getLogger(__name__)


def audit(db, user, action: str, detail: dict | None = None, request=None) -> None:
    """写一条审计日志。user 可为 None（如登录失败时还没取到用户）。"""
    try:
        ip = None
        if request is not None:
            fwd = request.headers.get("x-forwarded-for")
            ip = (fwd.split(",")[0].strip() if fwd else request.client.host) if fwd or request.client else None
        db.add(AuditLog(
            tenant_id=user.tenant_id if user else None,
            user_id=user.username if user else "anonymous",
            action=action,
            detail=detail or {},
            ip=ip,
        ))
        db.commit()
    except Exception as e:  # noqa: BLE001 —— 审计失败不能拖垮业务
        logger.warning("审计日志写入失败: %s", e)
        db.rollback()

"""审计日志查询 API（仅管理员）。

安全设计：
- require_admin 依赖：非 admin 直接 403（权限体系的落点）
- admin 默认只看【自己租户】的日志（多租户隔离同样约束管理员看数据）
- 分页 + 动作过滤：日志量大时不能全量拉（演示数据量小，分页仍做了）

【为什么 admin 也按租户隔离？】
多租户产品里，管理员通常也只管自己企业。除非是平台级超管
（跨租户管理），否则不该看到别的租户的数据——包括审计日志。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.models import AuditLog, User
from app.security import require_admin

router = APIRouter(prefix="/api/audit", tags=["audit"])


def get_db():
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/logs")
def list_logs(
    limit: int = 50,
    offset: int = 0,
    action: str | None = None,
    user: User = Depends(require_admin),  # 只有 admin 能看审计
    db: Session = Depends(get_db),
):
    """查询审计日志（倒序：最新在前）。可按动作过滤 + 分页。"""
    q = db.query(AuditLog).filter_by(tenant_id=user.tenant_id)  # 租户隔离
    if action:
        q = q.filter_by(action=action)
    rows = q.order_by(AuditLog.id.desc()).offset(offset).limit(limit).all()
    return {
        "total": q.count(),
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "action": r.action,
                "detail": r.detail,
                "ip": r.ip,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/actions")
def list_actions(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """审计动作清单（前端过滤下拉用）。"""
    rows = (db.query(AuditLog.action)
            .filter_by(tenant_id=user.tenant_id)
            .distinct().all())
    return {"actions": sorted(r[0] for r in rows)}

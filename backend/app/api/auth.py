"""认证 API：登录签发 token / 获取当前登录用户。

鉴权模型（面试可讲）：
- 无状态 token：服务端不存 session，只验签名和过期 → 天然支持横向扩容
  （任何实例都能验同一把密钥签的 token，不用共享 session 存储）
- 密码只存哈希：数据库泄露也拿不到明文（PBKDF2 + 盐 + 10万次迭代）
- 登录失败也记审计：安全运营要看到"谁在试密码"（暴力破解检测）
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.models import User
from app.schemas import LoginRequest, TokenResponse, UserOut
from app.security import (
    create_token, get_current_user, hash_password, verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def get_db():
    """数据库会话依赖（本路由用，避免跨模块耦合 conversations.get_db）。"""
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """登录：验证用户名+密码 → 返回 token + 用户信息。

    失败统一返回 401（不区分"用户不存在"和"密码错误"——
    区分了等于告诉攻击者"这个用户名存在"，帮助他撞库）。
    """
    from app.services.audit_service import audit
    user = db.query(User).filter_by(username=body.username).first()
    # 统一失败话术：不泄露"用户名是否存在"（安全最佳实践）
    if user is None or not verify_password(body.password, user.password_hash):
        audit(db, None, "login_failed", {"username": body.username}, request=request)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.active:
        audit(db, user, "login_failed", {"username": body.username, "reason": "disabled"}, request=request)
        raise HTTPException(status_code=401, detail="账号已被禁用")
    token = create_token(user.id)
    audit(db, user, "login", {}, request=request)
    return TokenResponse(
        token=token,
        user=UserOut(
            id=user.id, username=user.username, display_name=user.display_name,
            role=user.role, tenant_id=user.tenant_id,
        ),
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    """当前登录用户信息（前端刷新页面恢复登录态用）。"""
    return UserOut(
        id=user.id, username=user.username, display_name=user.display_name,
        role=user.role, tenant_id=user.tenant_id,
    )

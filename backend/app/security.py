"""安全模块：密码哈希 / Token 签发校验 / FastAPI 依赖注入（鉴权）。

【为什么不用 JWT 库 / bcrypt？】
- 本项目追求"零新依赖"：HMAC-SHA256 签名 token + PBKDF2 密码哈希，
  全用 Python 标准库实现，原理透明，面试能讲清楚每一步。
- 换 JWT（如 pyjwt）只是换签发函数，调用方不变（对称的"签名+校验"模式）。

【鉴权流程】
1. 登录：POST /api/auth/login 验证密码 → 签发 token（内含 user_id + 过期时间 + 签名）
2. 后续请求：前端带 `Authorization: Bearer <token>`
3. 依赖注入 get_current_user：解 token → 查用户 → 返回 User 对象（未登录/过期/禁用 → 401）
4. 需要管理员的操作：再加一层 require_admin 依赖（非 admin → 403）

【多租户的关键设计】
- token 只认"用户是谁"，不直接存租户；租户从用户表的 tenant_id 查。
  好处：改租户只改数据库，已签发的 token 自动生效（无需重新登录/重新签发）。
"""
import hashlib
import hmac
import json
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import User

# Token 有效期（小时）
TOKEN_TTL_HOURS = 24

# ===== 密码哈希（PBKDF2：加盐 + 迭代拉伸，防彩虹表/暴力破解）=====
def hash_password(password: str) -> str:
    """哈希密码。格式: pbkdf2$<salt>$<hash>（盐随机，同密码两次哈希不同）。"""
    salt = secrets.token_hex(16)
    # 10 万次迭代：把密码"拉伸"，让暴力破解每猜一次都要算 10 万次哈希
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码。从存储串里拆出盐，重新计算哈希比对。"""
    try:
        _, salt, expected = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    # hmac.compare_digest：常数时间比较，防"时序攻击"（比较耗时差异泄露信息）
    return hmac.compare_digest(dk.hex(), expected)


# ===== Token（HMAC-SHA256 签名，格式: payload.signature）=====
def _b64encode(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(s: str) -> bytes:
    # urlsafe_b64decode 需要补回 padding（rstrip 掉的 =）
    return urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: str) -> str:
    """HMAC-SHA256 签名：只有持有 SECRET_KEY 的人才能伪造。"""
    return hmac.new(
        settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def create_token(user_id: int) -> str:
    """签发 token：payload(JSON) + 签名。过期时间 24h。"""
    payload = _b64encode(json.dumps({
        "uid": user_id,
        "exp": int(time.time()) + TOKEN_TTL_HOURS * 3600,
    }).encode())
    return f"{payload}.{_sign(payload)}"


def decode_token(token: str) -> int | None:
    """解 token → user_id。签名不匹配 / 过期 / 格式错 → None。

    校验顺序：先验签名（防伪造），再验过期（防复用）。
    """
    try:
        payload, sig = token.split(".")
        if not hmac.compare_digest(sig, _sign(payload)):
            return None  # 签名不对：token 被篡改过
        data = json.loads(_b64decode(payload))
        if data.get("exp", 0) < time.time():
            return None  # 已过期
        return int(data["uid"])
    except (ValueError, json.JSONDecodeError, KeyError, TypeError):
        return None


# ===== FastAPI 依赖注入（鉴权核心）=====
# HTTPBearer 从 Authorization: Bearer <token> 提取凭据（自动处理 401/403 语义）
_bearer = HTTPBearer(auto_error=False)


def get_db():
    """数据库会话依赖（供 auth 路由用，避免循环 import conversations.get_db）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """依赖注入：从请求头解析 token → 返回当前登录用户。

    用法：def create_conv(user: User = Depends(get_current_user)): ...
    未登录（无 token/过期/伪造）→ 401；账号被禁用 → 401。
    """
    if creds is None:
        raise HTTPException(status_code=401, detail="未登录，请先登录")
    uid = decode_token(creds.credentials)
    if uid is None:
        raise HTTPException(status_code=401, detail="登录已过期或 token 无效")
    user = db.get(User, uid)
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="账号不存在或已被禁用")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """依赖注入：要求管理员权限。用法：user = Depends(require_admin)。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def client_ip(request: Request) -> str | None:
    """提取客户端 IP（审计日志用）。真实部署在反向代理后应读 X-Forwarded-For。"""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None

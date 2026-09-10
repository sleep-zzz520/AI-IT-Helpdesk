"""本地监控服务（模拟真实企业 VPN 证书监控系统）。

【为什么存在】
真实企业监控系统（如自建 CMDB、堡垒机、VPN 网关管理后台）我们接触不到，
但 Agent 必须证明"能对接真实 HTTP 系统"而不是读内存字典。
这个服务用 FastAPI 起一个真实的 HTTP 服务，接口契约完全模拟企业监控系统：
- /api/v1/cert/{username}   GET    查询证书状态
- /api/v1/cert/{username}   POST   续期证书

它和真实系统的唯一区别只是"数据是假的"，网络栈、HTTP 协议、错误处理全真实。
换真实系统时：Agent 侧只改 MONITOR_BASE_URL，这个服务就不需要了。

【运行】
    cd backend && uvicorn mock_monitor.server:app --port 9100 --reload

【数据】
用 SQLite 持久化（演示用，真实系统是 MySQL/PG）。
账号初始化与原 MOCK_USERS 一致，保证现有 e2e 测试不破坏。
"""
import json
import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = Path(__file__).resolve().parent / "monitor.db"

# 初始账号数据（与原 MOCK_USERS 对齐，保证 e2e 回归不破）
# 注意：必须与登录体系 users 表种子账号对齐（admin/zhangsan/lisi）——
# verify 节点用登录用户名查证书，监控系统没有对应账号会误报"账号不存在"
INITIAL_USERS = {
    "admin": {"cert_valid_until": "2026-07-30", "expired": True},  # 管理员也有证书（演示可测）
    "zhangsan": {"cert_valid_until": "2026-07-30", "expired": True},
    "lisi": {"cert_valid_until": "2027-01-15", "expired": False},
    "error_user": {"cert_valid_until": "2026-07-30", "expired": True},
}

app = FastAPI(title="Mock VPN Monitor", version="1.0.0")
# 允许本地调试跨域（演示用，真实系统在内网不开放 CORS）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 模拟网络抖动（可选，演示真实网络的不可靠性）----------
LATENCY_JITTER_MS = int(os.getenv("MONITOR_JITTER_MS", "0"))
FAILURE_RATE = float(os.getenv("MONITOR_FAILURE_RATE", "0"))


def _maybe_sleep_and_fail():
    """模拟网络延迟 + 随机失败（演示 Agent 侧重试/兜底的价值）。

    真实环境里监控 API 偶发超时很常见，Agent 必须能扛住。
    默认关闭（0），需要演示时设环境变量。
    """
    if LATENCY_JITTER_MS:
        import time
        time.sleep(random.uniform(0, LATENCY_JITTER_MS) / 1000)
    if FAILURE_RATE and random.random() < FAILURE_RATE:
        raise HTTPException(status_code=503, detail="monitor service temporarily unavailable")


# ---------- DB ----------
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """首次启动建表 + 灌初始数据。"""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cert_status (
                username        TEXT PRIMARY KEY,
                cert_valid_until TEXT NOT NULL,
                expired         INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS renewal_operations (
                idempotency_key TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                response_json TEXT NOT NULL
            )
        """)
        # 仅首次灌数据（已存在则跳过，保留续期后的状态）
        cur = conn.execute("SELECT COUNT(*) FROM cert_status")
        if cur.fetchone()[0] == 0:
            for u, info in INITIAL_USERS.items():
                conn.execute(
                    "INSERT INTO cert_status VALUES (?, ?, ?)",
                    (u, info["cert_valid_until"], int(info["expired"])),
                )


@app.on_event("startup")
def _startup():
    init_db()


# ---------- Schemas ----------
class CertStatus(BaseModel):
    status: str
    expired: bool
    cert_valid_until: str


class RenewRequest(BaseModel):
    days: int = 180  # 续期天数，默认半年


class RenewResponse(BaseModel):
    status: str
    message: str
    new_valid_until: str


# ---------- API ----------
@app.get("/api/v1/cert/{username}", response_model=CertStatus)
def get_cert(username: str):
    """查证书状态。不存在 → 404（真实系统语义）。"""
    _maybe_sleep_and_fail()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM cert_status WHERE username = ?", (username,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"账号 {username} 不存在")
    return CertStatus(
        status="ok",
        expired=bool(row["expired"]),
        cert_valid_until=row["cert_valid_until"],
    )


@app.post("/api/v1/cert/{username}/renew", response_model=RenewResponse)
def renew_cert(
    username: str,
    req: RenewRequest,
    idempotency_key: str | None = Header(default=None),
):
    """续期证书。error_user 演示续期失败（真实系统也会有权限/服务不可达等失败）。"""
    _maybe_sleep_and_fail()
    with get_conn() as conn:
        if idempotency_key:
            previous = conn.execute(
                "SELECT username, response_json FROM renewal_operations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if previous is not None:
                if previous["username"] != username:
                    raise HTTPException(status_code=409, detail="操作编号已用于其他账号")
                return RenewResponse(**json.loads(previous["response_json"]))

        row = conn.execute(
            "SELECT * FROM cert_status WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"账号 {username} 不存在")

        # error_user 永远续期失败（演示 Agent 执行失败兜底转人工）
        if username == "error_user":
            raise HTTPException(status_code=503, detail="证书服务当前不可达，续期失败")

        new_date = (datetime.now() + timedelta(days=req.days)).strftime("%Y-%m-%d")
        conn.execute(
            "UPDATE cert_status SET cert_valid_until = ?, expired = 0 WHERE username = ?",
            (new_date, username),
        )
        response = {
            "status": "ok",
            "message": f"已为用户 {username} 续期证书 {req.days} 天",
            "new_valid_until": new_date,
        }
        if idempotency_key:
            conn.execute(
                "INSERT INTO renewal_operations (idempotency_key, username, response_json) VALUES (?, ?, ?)",
                (idempotency_key, username, json.dumps(response, ensure_ascii=False)),
            )
    return RenewResponse(**response)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/admin/reset")
def reset_data():
    """重置为初始数据（测试用，避免续期后状态污染下一次测试）。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM cert_status")
        conn.execute("DELETE FROM renewal_operations")
        for u, info in INITIAL_USERS.items():
            conn.execute(
                "INSERT INTO cert_status VALUES (?, ?, ?)",
                (u, info["cert_valid_until"], int(info["expired"])),
            )
    return {"status": "ok", "message": "数据已重置"}

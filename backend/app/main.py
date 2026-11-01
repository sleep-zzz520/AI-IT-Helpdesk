"""FastAPI 入口：后端服务的门面，所有 HTTP 接口从这里挂载。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.feedback import router as feedback_router
from app.api.kb import router as kb_router
from app.api.llm_configs import router as llm_configs_router
from app.config import settings
from app.db import init_db
from app.logging_config import setup_logging
from app.services.agent_ingress import AgentIngressMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时自动建表（容器化部署必需：无手动初始化步骤）。"""
    setup_logging()  # 统一日志：级别可配 + 文件轮转（生产排障可查）
    init_db()
    if settings.KB_SYNC_MODE == "git":
        import threading

        from app.rag.generations import run_worker
        stop = threading.Event()
        worker = threading.Thread(target=run_worker, args=(stop,), daemon=True, name="kb-sync-worker")
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=3)
    else:
        yield


app = FastAPI(title="智能IT运维服务台", version="0.4.0", lifespan=lifespan)
app.add_middleware(AgentIngressMiddleware)

# CORS：开发期允许所有来源（前端 Vite dev server 不同端口）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载业务路由
app.include_router(auth_router)          # 登录/当前用户
app.include_router(audit_router)         # 审计日志（admin only）
app.include_router(conversations_router)
app.include_router(feedback_router)
app.include_router(kb_router)
app.include_router(llm_configs_router)


@app.get("/health")
def health():
    """健康检查：确认服务、模型配置、数据库配置都就位。"""
    result = {
        "status": "ok",
        "model": settings.GLM_MODELS[0],  # 主模型（链首）
        "db": settings.MYSQL_DB,
    }
    if settings.KB_SYNC_MODE == "git":
        from app.rag.generations import state
        current = state()
        result.update(kb_mode="git", kb_generation=current["active_generation"],
                      kb_source_commit=current["source_commit"], kb_documents=current["doc_count"])
    return result

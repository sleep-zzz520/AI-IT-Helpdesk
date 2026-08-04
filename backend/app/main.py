"""FastAPI 入口：后端服务的门面，所有 HTTP 接口从这里挂载。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.conversations import router as conversations_router
from app.config import settings
from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时自动建表（容器化部署必需：无手动初始化步骤）。"""
    init_db()
    yield


app = FastAPI(title="智能IT运维服务台", version="0.4.0", lifespan=lifespan)

# CORS：开发期允许所有来源（前端 Vite dev server 不同端口）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载业务路由
app.include_router(conversations_router)


@app.get("/health")
def health():
    """健康检查：确认服务、模型配置、数据库配置都就位。"""
    return {
        "status": "ok",
        "model": settings.GLM_MODEL,
        "db": settings.MYSQL_DB,
    }

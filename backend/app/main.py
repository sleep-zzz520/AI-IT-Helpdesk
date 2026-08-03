"""FastAPI 入口：后端服务的门面，所有 HTTP 接口从这里挂载。"""
from fastapi import FastAPI

from app.config import settings

app = FastAPI(title="智能IT运维服务台", version="0.1.0")


@app.get("/health")
def health():
    """健康检查：确认服务、模型配置、数据库配置都就位。"""
    return {
        "status": "ok",
        "model": settings.GLM_MODEL,
        "db": settings.MYSQL_DB,
    }

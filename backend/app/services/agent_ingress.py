"""在认证与数据库依赖执行前限制 Agent HTTP 在途请求，防止连接池饥饿。"""
import re

from fastapi import HTTPException
from starlette.responses import JSONResponse

from app.services.agent_admission import acquire_ingress

# FastAPI 在路径参数校验前也可能执行认证依赖；+1/-1/非法 ID 均要先保护入口。
_MESSAGE_PATH = re.compile(r"^/api/conversations/[^/]+/messages/?$")


class AgentIngressMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or not _MESSAGE_PATH.fullmatch(scope.get("path", "")):
            return await self.app(scope, receive, send)
        try:
            lease = acquire_ingress()
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
            return await response(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            lease.release()

"""直接ASGI持有权闸门，保留流式响应和断连信号原样传递。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from xuanpu_server.platform import Platform


class InstanceMiddleware:
    """丢锁立即拒绝新工作，不引入额外的接收任务。"""

    def __init__(self, app: ASGIApp, platform: Platform) -> None:
        """装配时注入实例状态，指标路径保留用于诊断。"""
        self._app = app
        self._platform = platform

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """不包装receive，避免破坏助手路由对http.disconnect的处理。"""
        if scope["type"] == "http" and scope["path"] != "/metrics" and not self._platform.available:
            response = JSONResponse({"error": {"code": "runtime_unavailable",
                "message": "平台未持有运行权，请重新启动"}}, status_code=503)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)

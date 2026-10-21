"""在框架解析 JSON 前限制财务请求体内存占用。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimit:
    """先有界接收再交框架，适用无认证和分块请求。"""

    def __init__(self, app: ASGIApp, max_bytes: int = 1024 * 1024) -> None:
        """固定内部 API 上限，发票原字节通过受限文件桥传输。"""
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """超限立即 413，不继续消费请求也不拼接超限字节。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                response = JSONResponse(
                    {
                        "code": "request_too_large",
                        "message": "请求体超过财务接口上限",
                        "trace_id": "body-limit",
                        "retryable": False,
                    },
                    status_code=413,
                )
                await response(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay() -> Message:
            """只重放已受限的请求体，后续保留真实断连行为。"""
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

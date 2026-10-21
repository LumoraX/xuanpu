"""分块上限必须在框架解析和身份验证之前生效。"""

from __future__ import annotations

from typing import Any

import pytest
from xuanpu_finance.limits import RequestBodyLimit


@pytest.mark.parametrize("headers", [[], [(b"authorization", b"Bearer test-service")]])
async def test_chunked_body_is_bounded_before_application(headers: Any) -> None:
    """超过上限立即停止读取，不调用鉴权或 JSON 解析。"""
    called, reads, sent = False, 0, []

    async def application(scope: Any, receive: Any, send: Any) -> None:
        """任何大体积请求都不应到达业务框架。"""
        nonlocal called
        called = True

    async def receive() -> Any:
        """连续分块模拟无 Content-Length 的不受信输入。"""
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 65536, "more_body": True}

    async def send(message: Any) -> None:
        """记录实际 ASGI 状态。"""
        sent.append(message)

    await RequestBodyLimit(application)({"type": "http", "headers": headers}, receive, send)
    assert not called and reads == 17
    assert sent[0]["status"] == 413

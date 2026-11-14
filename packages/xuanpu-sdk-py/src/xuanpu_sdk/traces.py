"""本人执行追踪（R5-B）的只读客户端：trace 列表、执行树与瀑布，复用原认证和传输。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .client import _Http


def trace_path(trace_id: str) -> str:
    """标识只占一个路径段，不允许嵌入查询或其他资源路径。"""
    return "/v1/traces/" + quote(trace_id, safe="")


class TraceClient:
    """主客户端继承同一传输；只读，不提供任何写入口。"""

    _http: _Http

    async def list_traces(
        self, *, active: bool | None = None, cursor: str | None = None, limit: int = 50,
    ) -> dict[str, Any]:
        """一页本人 trace：``items``、``next_cursor``、与当前页无关的 ``total``。"""
        params: dict[str, Any] = {"limit": limit}
        if active is not None:
            params["active"] = "true" if active else "false"
        if cursor is not None:
            params["cursor"] = cursor
        return await self._http.request("GET", "/v1/traces", params=params)

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        """一条 trace 的执行树（只含元数据）。"""
        return await self._http.request("GET", trace_path(trace_id))

    async def trace_waterfall(self, trace_id: str) -> dict[str, Any]:
        """一条 trace 的瀑布行；运行中的行 ``duration_ms`` 为 None。"""
        return await self._http.request("GET", trace_path(trace_id) + "/waterfall")

    async def follow_trace(
        self, trace_id: str, *, after_seq: int = 0,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """跟随一条 trace 的增量：``snapshot`` → ``spans``… → ``end``。

        ``after_seq`` 是上次收到的事件序号（重连用）；与服务端当前序号相同时不重发快照。
        """
        headers = {"Last-Event-ID": str(after_seq)} if after_seq else None
        async for item in self._http.stream_get(trace_path(trace_id) + "/events", headers=headers):
            yield item

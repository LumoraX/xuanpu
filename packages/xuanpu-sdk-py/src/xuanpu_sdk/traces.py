"""本人执行追踪（R5-B）的只读客户端与管家入口（R5-C）：复用原认证和传输。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .client import _Http


# 管家一轮在服务端最长 90 秒（安排编组这类慢的一轮在真实模型下接近上限）；
# 这个请求至少等这么久，比 SDK 默认的 60 秒长，否则服务端还在处理，客户端先断开。
BUTLER_TIMEOUT_SECONDS = 120.0


def trace_path(trace_id: str) -> str:
    """标识只占一个路径段，不允许嵌入查询或其他资源路径。"""
    return "/v1/traces/" + quote(trace_id, safe="")


class TraceClient:
    """主客户端继承同一传输；只读，不提供任何写入口。"""

    _http: _Http

    async def send_to_butler(
        self, text: str, *, request_id: str, timezone: str = "Asia/Shanghai",
        input_files: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """对管家说一句话（``POST /v1/assistant/messages``），返回回执。

        ``request_id`` 必填：同键同体回原回执，同键不同体服务端返回 409。回执 ``mode`` 为
        ``butler``（含 ``trace_id`` 与 ``dispatches``）或 ``route``（租户未开启管家）。
        管家派工不等匠人结果。
        """
        body: dict[str, Any] = {"request_id": request_id, "text": text, "timezone": timezone}
        if input_files:
            body["input_files"] = input_files
        # 取客户端超时与管家下限中较长的一个：调用方显式给了更长的超时就照用
        timeout = httpx.Timeout(max(self._http.timeout, BUTLER_TIMEOUT_SECONDS), connect=30.0)
        return await self._http.request("POST", "/v1/assistant/messages", json=body,
                                        timeout=timeout)

    async def list_butler_pending(self) -> list[dict[str, Any]]:
        """本人仍待处理的项（``GET /v1/assistant/pending``）：匠人的问题与需要审批的提示。"""
        body = await self._http.request("GET", "/v1/assistant/pending")
        return list(body["items"])

    async def answer_butler_pending(self, pending_id: str, text: str, *, request_id: str,
                                    ) -> dict[str, Any]:
        """答复一个问题项，原话转给匠人；同一 ``request_id`` 重试回原结果，审批类项返回 409。"""
        return await self._http.request(
            "POST", "/v1/assistant/pending/" + quote(pending_id, safe="") + "/answer",
            json={"request_id": request_id, "text": text})

    async def get_butler_group(self, group_id: str) -> dict[str, Any]:
        """本人一个编组的进展（``GET /v1/assistant/groups/{id}``）：成员、追问与各步裁决。"""
        return await self._http.request("GET", "/v1/assistant/groups/" + quote(group_id, safe=""))

    async def list_group_messages(self, group_id: str) -> list[dict[str, Any]]:
        """会诊消息区（只读，按序号）：成员发布的结论与追问回复、管家的收口说明。"""
        body = await self._http.request(
            "GET", "/v1/assistant/groups/" + quote(group_id, safe="") + "/messages")
        return list(body["items"])

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

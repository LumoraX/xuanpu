"""执行追踪的 SSE 增量流：首帧快照，之后只推变化的 span，trace 结束即收尾。

事件 ``id`` 是 trace 已应用的最大事实序号。断线重连带 ``Last-Event-ID``：与当前序号相同则
不重发快照，落后则重发一次完整快照补齐（存储不按 span 记序号，快照是唯一可靠的补齐方式）。
每轮重新认证当前员工；身份失效、被停用或换人即结束流。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import PlatformError
from xuanpu_server.tracing.views import span_view, trace_view

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from xuanpu_server.tracing.models import TraceOwner, TraceView
    from xuanpu_server.tracing.store import SpanStore

POLL_SECONDS = 1.0


def _frame(event: str, seq: int, data: dict[str, Any]) -> str:
    """一帧 SSE。"""
    return f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _header(view: TraceView) -> dict[str, Any]:
    """帧里携带的 trace 头：完整性与是否仍在运行。"""
    return {**trace_view(view.trace), "active": view.active}


async def trace_frames(
    store: SpanStore, owner: TraceOwner, trace_id: str, *,
    after_seq: int, reauth: Callable[[], Awaitable[TraceOwner]],
    disconnected: Callable[[], Awaitable[bool]], poll_seconds: float = POLL_SECONDS,
) -> AsyncIterator[str]:
    """生成 SSE 帧。

    Args:
        store: span 存储。
        owner: 建流时已认证的归属。
        trace_id: 要跟随的 trace。
        after_seq: 客户端已有状态对应的序号；0 表示没有状态。
        reauth: 每轮重新认证，返回当前归属；失效时抛 ``PlatformError``。
        disconnected: 客户端是否已断开。
        poll_seconds: 轮询间隔。
    """
    try:
        view = await store.get(owner, trace_id)
    except PlatformError:
        return
    known = {span.span_id: span_view(span) for span in view.spans}
    seq = view.trace.last_seq
    header = _header(view)
    if after_seq != seq or after_seq == 0:
        yield _frame("snapshot", seq, {**header, "spans": list(known.values())})
    while view.active:
        yield ": heartbeat\n\n"
        await asyncio.sleep(poll_seconds)
        if await disconnected():
            return
        try:
            current = await reauth()
            if current != owner:
                return
            view = await store.get(owner, trace_id)
        except PlatformError:
            return
        changed = [v for v in map(span_view, view.spans) if known.get(v["span_id"]) != v]
        fresh = _header(view)
        if changed or fresh != header:
            known.update((v["span_id"], v) for v in changed)
            seq, header = view.trace.last_seq, fresh
            yield _frame("spans", seq, {**header, "spans": changed})
    yield _frame("end", seq, header)

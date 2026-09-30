"""把一轮对话的事件流写成 Server-Sent Events。"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.audit import SENSITIVE_KINDS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import taifeng

    from xuanpu_server.runtime.sessions import Turn

# 长时间没有事件时发一行注释，防止中间的代理因空闲断开连接
KEEPALIVE_SECONDS = 15.0


def frame(event: str, data: dict[str, Any], *, event_id: str | None = None) -> bytes:
    """一个 SSE 帧。"""
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, default=str))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def event_payload(event: taifeng.EventMsg) -> dict[str, Any]:
    """一条内核事件的对外表示。"""
    return {
        "submission_id": event.submission_id,
        "seq": event.seq,
        "at": event.timestamp.timestamp(),
        "kind": event.msg.kind,
        "data": event.msg.data,
    }


async def stream_turn(turn: Turn) -> AsyncIterator[bytes]:
    """逐帧产出一轮对话的事件，直到这一轮结束。

    第一帧是 ``turn``，告诉调用方本轮的 ``submission_id``；之后每条内核事件一帧，事件名
    就是事件类型；结束时补一帧 ``end``。含完整提示词的事件不外发。
    """
    yield frame("turn", {"session_id": turn.session_id, "submission_id": turn.submission_id})
    source = turn.stream.events().__aiter__()
    pending: asyncio.Future[taifeng.EventMsg] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(source))
            done, _ = await asyncio.wait({pending}, timeout=KEEPALIVE_SECONDS)
            if not done:
                yield b": keepalive\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                break
            finally:
                pending = None
            if event.msg.kind in SENSITIVE_KINDS:
                continue
            yield frame(event.msg.kind, event_payload(event), event_id=str(event.seq))
    finally:
        if pending is not None:
            pending.cancel()
    yield frame("end", {"submission_id": turn.submission_id, "overflowed": turn.stream.overflowed})


__all__ = ["KEEPALIVE_SECONDS", "event_payload", "frame", "stream_turn"]

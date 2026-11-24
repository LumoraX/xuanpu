"""执行追踪 SSE：首帧快照、只推变化、结束收尾、重连补齐、身份失效断流。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from server.api.trace_events import trace_frames
from server.core.errors import AuthenticationError
from server.engine.tracing.memory import MemorySpanStore

from .helpers import ALICE, BOB, T0, fact

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from server.engine.tracing.models import TraceOwner


def _parse(frame: str) -> tuple[str, int, dict[str, Any]]:
    """拆出事件名、序号与数据。"""
    lines = dict(line.split(": ", 1) for line in frame.strip().splitlines())
    return lines["event"], int(lines["id"]), json.loads(lines["data"])


async def _store() -> MemorySpanStore:
    """Alice 有一条运行中的 trace：根 span 加一个工具 span。"""
    store = MemorySpanStore()
    await store.open_trace(ALICE, trace_id="trc_1", origin="user", title="查工单", at=T0)
    await store.apply(ALICE, "trc_1", [
        fact(1, "root", 0, kind="artisan", name="工单", state="running"),
        fact(2, "tool", 10, kind="tool", name="crm.query", parent_span_id="root", state="running"),
    ])
    return store


def _stream(store: MemorySpanStore, *, after: int = 0, who: list[TraceOwner] | None = None,
            ) -> AsyncIterator[str]:
    """以 Alice 建流；``who`` 可在中途替换重认证结果。"""
    current = who if who is not None else [ALICE]

    async def reauth() -> TraceOwner:
        """返回当前身份；空表示凭据已失效。"""
        if not current:
            raise AuthenticationError("凭据失效")
        return current[0]

    async def disconnected() -> bool:
        """客户端始终在线。"""
        return False

    return trace_frames(store, ALICE, "trc_1", after_seq=after, reauth=reauth,
                        disconnected=disconnected, poll_seconds=0)


async def test_snapshot_then_only_changes_then_end() -> None:
    """首帧完整；之后只推变化的 span；全部终态后发 end 并结束。"""
    store = await _store()
    stream = _stream(store)
    event, seq, data = _parse(await anext(stream))
    assert (event, seq, data["active"]) == ("snapshot", 2, True)
    assert {s["span_id"] for s in data["spans"]} == {"root", "tool"}
    assert await anext(stream) == ": heartbeat\n\n"
    await store.apply(ALICE, "trc_1", [fact(3, "tool", 50, state="completed")])
    event, seq, data = _parse(await anext(stream))
    assert (event, seq) == ("spans", 3)
    assert [s["span_id"] for s in data["spans"]] == ["tool"]
    assert not {"arguments", "output", "text", "content", "tenant_id"} & set(data["spans"][0])
    await anext(stream)
    await store.apply(ALICE, "trc_1", [fact(4, "root", 90, state="completed")])
    event, _, data = _parse(await anext(stream))
    assert event == "spans" and data["active"] is False
    event, seq, _ = _parse(await anext(stream))
    assert (event, seq) == ("end", 4)
    assert [frame async for frame in stream] == []


async def test_reconnect_up_to_date_skips_snapshot_behind_gets_one() -> None:
    """重连时序号一致不重发快照；落后则重发完整快照补齐。"""
    store = await _store()
    assert await anext(_stream(store, after=2)) == ": heartbeat\n\n"
    event, seq, data = _parse(await anext(_stream(store, after=1)))
    assert (event, seq, len(data["spans"])) == ("snapshot", 2, 2)


async def test_stream_ends_when_identity_lapses_or_changes() -> None:
    """凭据失效或换人：下一轮直接结束，不再推送。"""
    for after in ([], [BOB]):
        store = await _store()
        who = [ALICE]
        stream = _stream(store, who=who)
        await anext(stream)
        await anext(stream)
        who[:] = after
        await store.apply(ALICE, "trc_1", [fact(3, "tool", 50, state="completed")])
        assert [frame async for frame in stream] == []


async def test_other_owner_gets_nothing() -> None:
    """不属于本人的 trace 不产出任何帧（接入层在建流前已返回 404）。"""
    store = await _store()

    async def reauth() -> TraceOwner:
        """不会被调用。"""
        return BOB

    async def disconnected() -> bool:
        """在线。"""
        return False

    stream = trace_frames(store, BOB, "trc_1", after_seq=0, reauth=reauth,
                          disconnected=disconnected, poll_seconds=0)
    assert [frame async for frame in stream] == []

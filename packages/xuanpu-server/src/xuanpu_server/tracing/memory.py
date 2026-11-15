"""span 存储的内存实现：用于契约测试与本机试用，行为与 PostgreSQL 实现一致。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio

from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.tracing.apply import apply_batch
from xuanpu_server.tracing.models import DEFAULT_SOURCE, TERMINAL_STATES, TraceRecord, TraceView
from xuanpu_server.tracing.store import (
    PAGE_DEFAULT,
    TracePage,
    check_limit,
    decode_cursor,
    encode_cursor,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from xuanpu_server.tracing.models import SpanFact, SpanRecord, TraceOrigin, TraceOwner


@dataclass
class _Entry:
    """一条 trace 的可变容器；只在锁内修改。"""

    record: TraceRecord
    spans: dict[str, SpanRecord] = field(default_factory=dict)


class MemorySpanStore:
    """进程内实现；一把锁串行化全部写入。"""

    def __init__(self, *, max_spans_per_trace: int = 2048, max_attempts_per_span: int = 64) -> None:
        """上限由配置注入。"""
        self._max_spans = max_spans_per_trace
        self._max_attempts = max_attempts_per_span
        self._traces: dict[str, _Entry] = {}
        self._lock = anyio.Lock()

    async def open_trace(
        self, owner: TraceOwner, *, trace_id: str, origin: TraceOrigin, title: str, at: datetime,
    ) -> TraceRecord:
        """幂等建 trace；标识被其他归属占用时报冲突，不泄露对方信息。"""
        async with self._lock:
            entry = self._traces.get(trace_id)
            if entry is not None:
                if entry.record.owner != owner:
                    raise ConflictError("trace 标识已被占用")
                return entry.record
            record = TraceRecord(
                trace_id=trace_id, owner=owner, origin=origin, title=title,
                created_at=at, updated_at=at,
            )
            self._traces[trace_id] = _Entry(record)
            return record

    async def apply(
        self, owner: TraceOwner, trace_id: str, facts: Sequence[SpanFact], *,
        source: str = DEFAULT_SOURCE,
        max_spans: int | None = None, max_attempts: int | None = None,
    ) -> TraceRecord:
        """按序号应用；不在锁外做任何解析。"""
        async with self._lock:
            entry = self._owned(owner, trace_id)
            result = apply_batch(
                entry.record, entry.spans, len(entry.spans), facts,
                source=source, max_spans=max_spans or self._max_spans,
                max_attempts=max_attempts or self._max_attempts,
            )
            entry.spans.update(result.changed)
            entry.record = result.record
            return entry.record

    async def mark_incomplete(self, owner: TraceOwner, trace_id: str) -> TraceRecord:
        """显式标不完整。"""
        async with self._lock:
            entry = self._owned(owner, trace_id)
            entry.record = entry.record.model_copy(update={"incomplete": True})
            return entry.record

    async def get(self, owner: TraceOwner, trace_id: str) -> TraceView:
        """返回快照；span 按开始时间与标识排序。"""
        async with self._lock:
            entry = self._owned(owner, trace_id)
            spans = sorted(entry.spans.values(), key=lambda s: (s.started_at, s.span_id))
            return TraceView(trace=entry.record, spans=tuple(spans))

    async def list(
        self, owner: TraceOwner, *, active: bool | None = None,
        cursor: str | None = None, limit: int = PAGE_DEFAULT,
    ) -> TracePage:
        """按创建时间倒序；总数在过滤后、分页前计算。"""
        check_limit(limit)
        after = decode_cursor(cursor) if cursor is not None else None
        async with self._lock:
            rows = [e for e in self._traces.values() if e.record.owner == owner
                    and (active is None or _is_active(e) == active)]
        rows.sort(key=lambda e: (e.record.created_at, e.record.trace_id), reverse=True)
        total = len(rows)
        if after is not None:
            rows = [e for e in rows if (e.record.created_at, e.record.trace_id) < after]
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1].record
            next_cursor = encode_cursor(last.created_at, last.trace_id)
        return TracePage(items=tuple(e.record for e in page), next_cursor=next_cursor, total=total)

    async def purge(self, tenant_id: str, *, before: datetime, batch: int = 500) -> int:
        """按批删除过期 trace。"""
        async with self._lock:
            doomed = [
                tid for tid, e in self._traces.items()
                if e.record.owner.tenant_id == tenant_id and e.record.created_at < before
            ][:batch]
            for trace_id in doomed:
                del self._traces[trace_id]
            return len(doomed)

    def _owned(self, owner: TraceOwner, trace_id: str) -> _Entry:
        """不存在与不属于本人同样报 NotFound。"""
        entry = self._traces.get(trace_id)
        if entry is None or entry.record.owner != owner:
            raise NotFoundError("trace 不存在")
        return entry


def _is_active(entry: _Entry) -> bool:
    """仍有未到终态的 span。"""
    return any(span.state not in TERMINAL_STATES for span in entry.spans.values())

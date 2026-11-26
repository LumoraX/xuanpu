"""派工记录的内存实现：单进程、一把锁，行为与 PostgreSQL 实现一致。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from server.core.errors import ConflictError, NotFoundError
from server.engine.butler.dispatch_store import check_capacity, check_owner, replay
from server.engine.butler.models import transition

if TYPE_CHECKING:
    from datetime import datetime

    from server.engine.butler.models import DispatchRecord, DispatchState
    from server.engine.tracing.models import TraceOwner


class MemoryDispatchStore:
    """按 ``(租户, 派工)`` 存记录。"""

    def __init__(self) -> None:
        """空存储。"""
        self._records: dict[tuple[str, str], DispatchRecord] = {}
        self._lock = asyncio.Lock()

    async def create(
        self, owner: TraceOwner, record: DispatchRecord, *,
        max_per_trace: int, max_active_traces: int,
    ) -> tuple[DispatchRecord, bool]:
        """先查幂等，再在同一把锁内核对容量后写入。"""
        check_owner(owner, record)
        async with self._lock:
            for existing in self._records.values():
                if (existing.tenant_id, existing.user_id, existing.request_id) == (
                        record.tenant_id, record.user_id, record.request_id):
                    return replay(existing, record), False
            if (record.tenant_id, record.dispatch_id) in self._records:
                raise ConflictError("派工标识已被占用")
            tenant = [r for r in self._records.values() if r.tenant_id == record.tenant_id]
            in_trace = [r for r in tenant if r.trace_id == record.trace_id]
            check_capacity(
                in_trace=len(in_trace), trace_active=any(r.active for r in in_trace),
                active_traces=len({r.trace_id for r in tenant if r.active}),
                max_per_trace=max_per_trace, max_active_traces=max_active_traces,
            )
            self._records[(record.tenant_id, record.dispatch_id)] = record
            return record, True

    async def get(self, owner: TraceOwner, dispatch_id: str) -> DispatchRecord:
        """他人的与不存在的一样 404。"""
        record = self._records.get((owner.tenant_id, dispatch_id))
        if record is None or record.user_id != owner.user_id:
            raise NotFoundError("派工不存在")
        return record

    async def list_for_trace(self, owner: TraceOwner, trace_id: str) -> tuple[DispatchRecord, ...]:
        """本人在这条 trace 下的派工，按创建先后。"""
        found = [r for r in self._records.values()
                 if (r.tenant_id, r.user_id, r.trace_id) == (owner.tenant_id, owner.user_id, trace_id)]
        return tuple(sorted(found, key=lambda r: (r.created_at, r.dispatch_id)))

    async def list_recent(self, owner: TraceOwner, *, limit: int = 20) -> tuple[DispatchRecord, ...]:
        """本人最近的派工记录，按创建时间倒序。"""
        found = [r for r in self._records.values()
                 if (r.tenant_id, r.user_id) == (owner.tenant_id, owner.user_id)]
        ordered = sorted(found, key=lambda r: (r.created_at, r.dispatch_id), reverse=True)
        return tuple(ordered[:limit])

    async def update(
        self, owner: TraceOwner, dispatch_id: str, *, expected_version: int,
        to: DispatchState, at: datetime, host_ref: str | None = None,
        end_reason: str | None = None,
    ) -> DispatchRecord:
        """版本相符才推进。"""
        async with self._lock:
            current = await self.get(owner, dispatch_id)
            if current.version != expected_version:
                raise ConflictError("派工已被更新，请重新读取")
            updated = transition(current, to, at=at, host_ref=host_ref, end_reason=end_reason)
            self._records[(owner.tenant_id, dispatch_id)] = updated
            return updated

    async def active(self, tenant_id: str, *, limit: int = 200) -> tuple[DispatchRecord, ...]:
        """进行中的派工，按创建先后。"""
        found = [r for r in self._records.values() if r.tenant_id == tenant_id and r.active]
        return tuple(sorted(found, key=lambda r: (r.created_at, r.dispatch_id))[:limit])

    async def find_by_host(self, owner: TraceOwner, host_ref: str) -> DispatchRecord | None:
        """本人承载在 ``host_ref`` 上的派工。"""
        for record in self._records.values():
            if (record.tenant_id, record.user_id, record.host_ref) == (
                    owner.tenant_id, owner.user_id, host_ref):
                return record
        return None

    async def active_traces(self, tenant_id: str) -> int:
        """有进行中派工的不同 trace 数。"""
        return len({r.trace_id for r in self._records.values()
                    if r.tenant_id == tenant_id and r.active})

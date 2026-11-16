"""唤醒事件与待本人处理项的存储契约及内存实现；PostgreSQL 实现见 ``wake_postgres``。

- 事件按 ``(租户, 派工, key)`` 去重；决定先写者为准；确认幂等。
- 待处理项按 ``(租户, 派工, source_key)`` 去重；改动须带期望版本。
- 员工读写一律按 ``(租户, 员工)`` 隔离，他人的与不存在的一样 404。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from xuanpu_server.butler.wake_models import OPEN_PENDING, ButlerEvent, PendingItem
from xuanpu_server.errors import ConflictError, NotFoundError

if TYPE_CHECKING:
    from datetime import datetime

    from xuanpu_server.butler.wake_models import WakeDecision
    from xuanpu_server.tracing.models import TraceOwner


class WakeStore(Protocol):
    """唤醒事件与待处理项存储。"""

    async def emit(self, event: ButlerEvent) -> tuple[ButlerEvent, bool]:
        """写一条事件；同键已存在则返回原事件与 False。"""
        ...

    async def unacked(self, tenant_id: str, *, limit: int = 50) -> tuple[ButlerEvent, ...]:
        """租户内尚未确认的事件，按写入先后。"""
        ...

    async def decide(self, tenant_id: str, event_id: str, decision: WakeDecision) -> ButlerEvent:
        """写入决定；已有决定时保持原决定并返回（先写者为准）。"""
        ...

    async def ack(self, tenant_id: str, event_id: str, at: datetime) -> None:
        """确认事件已处理完；重复确认无副作用。"""
        ...

    async def open_pending(self, item: PendingItem) -> tuple[PendingItem, bool]:
        """建待处理项；同键已存在则返回原项与 False。"""
        ...

    async def list_pending(self, owner: TraceOwner, *, open_only: bool = True,
                           dispatch_id: str | None = None) -> tuple[PendingItem, ...]:
        """本人的待处理项，按创建先后。"""
        ...

    async def get_pending(self, owner: TraceOwner, pending_id: str) -> PendingItem:
        """读一项本人的待处理项。"""
        ...

    async def update_pending(self, owner: TraceOwner, pending_id: str, *, expected_version: int,
                             at: datetime, **fields: Any) -> PendingItem:
        """版本相符才更新给定字段。"""
        ...


def bump(item: PendingItem, at: datetime, fields: dict[str, Any]) -> PendingItem:
    """更新字段、加版本；重建以校验新字段。"""
    return PendingItem.model_validate({**item.model_dump(), **fields, "updated_at": at,
                                       "version": item.version + 1})


class MemoryWakeStore:
    """单进程内存实现，一把锁。"""

    def __init__(self) -> None:
        """空存储。"""
        self._events: dict[tuple[str, str], ButlerEvent] = {}
        self._pending: dict[tuple[str, str], PendingItem] = {}
        self._lock = asyncio.Lock()

    async def emit(self, event: ButlerEvent) -> tuple[ButlerEvent, bool]:
        """同键去重。"""
        async with self._lock:
            for existing in self._events.values():
                if (existing.tenant_id, existing.dispatch_id, existing.key) == (
                        event.tenant_id, event.dispatch_id, event.key):
                    return existing, False
            self._events[(event.tenant_id, event.event_id)] = event
            return event, True

    async def unacked(self, tenant_id: str, *, limit: int = 50) -> tuple[ButlerEvent, ...]:
        """按写入先后。"""
        found = [e for e in self._events.values() if e.tenant_id == tenant_id and e.acked_at is None]
        return tuple(sorted(found, key=lambda e: (e.created_at, e.event_id))[:limit])

    async def decide(self, tenant_id: str, event_id: str, decision: WakeDecision) -> ButlerEvent:
        """先写者为准。"""
        async with self._lock:
            event = self._event(tenant_id, event_id)
            if event.decision is None:
                event = event.model_copy(update={"decision": decision})
                self._events[(tenant_id, event_id)] = event
            return event

    async def ack(self, tenant_id: str, event_id: str, at: datetime) -> None:
        """幂等确认。"""
        async with self._lock:
            event = self._event(tenant_id, event_id)
            if event.acked_at is None:
                self._events[(tenant_id, event_id)] = event.model_copy(update={"acked_at": at})

    def _event(self, tenant_id: str, event_id: str) -> ButlerEvent:
        """按标识取事件。"""
        event = self._events.get((tenant_id, event_id))
        if event is None:
            raise NotFoundError("唤醒事件不存在")
        return event

    async def open_pending(self, item: PendingItem) -> tuple[PendingItem, bool]:
        """同键去重。"""
        async with self._lock:
            for existing in self._pending.values():
                if (existing.tenant_id, existing.dispatch_id, existing.source_key) == (
                        item.tenant_id, item.dispatch_id, item.source_key):
                    return existing, False
            self._pending[(item.tenant_id, item.pending_id)] = item
            return item, True

    async def list_pending(self, owner: TraceOwner, *, open_only: bool = True,
                           dispatch_id: str | None = None) -> tuple[PendingItem, ...]:
        """本人的项。"""
        found = [p for p in self._pending.values()
                 if (p.tenant_id, p.user_id) == (owner.tenant_id, owner.user_id)
                 and (not open_only or p.state in OPEN_PENDING)
                 and (dispatch_id is None or p.dispatch_id == dispatch_id)]
        return tuple(sorted(found, key=lambda p: (p.created_at, p.pending_id)))

    async def get_pending(self, owner: TraceOwner, pending_id: str) -> PendingItem:
        """他人的与不存在的一样 404。"""
        item = self._pending.get((owner.tenant_id, pending_id))
        if item is None or item.user_id != owner.user_id:
            raise NotFoundError("待处理项不存在")
        return item

    async def update_pending(self, owner: TraceOwner, pending_id: str, *, expected_version: int,
                             at: datetime, **fields: Any) -> PendingItem:
        """版本相符才更新。"""
        async with self._lock:
            item = await self.get_pending(owner, pending_id)
            if item.version != expected_version:
                raise ConflictError("待处理项已被更新，请重新读取")
            updated = bump(item, at, fields)
            self._pending[(owner.tenant_id, pending_id)] = updated
            return updated

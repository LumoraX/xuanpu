"""唤醒事件与待本人处理项的 PostgreSQL 实现（迁移 0023）。

拼进 SQL 的只有来自 ``SqlNamespace`` 的表名，取值一律参数绑定。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from server.core.controlplane.schema import DEFAULT_NAMESPACE
from server.core.errors import ConflictError, NotFoundError
from server.engine.butler.wake_models import ButlerEvent, PendingItem
from server.engine.butler.wake_store import bump

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg
    from taifeng_stores import SqlNamespace

    from server.engine.butler.wake_models import WakeDecision
    from server.engine.tracing.models import TraceOwner


class PostgresWakeStore:
    """与控制面共用连接池与命名空间。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名由命名空间决定。"""
        self._pool = pool
        self._events = namespace.table("butler_event")
        self._pending = namespace.table("butler_pending")

    async def emit(self, event: ButlerEvent) -> tuple[ButlerEvent, bool]:
        """同键去重：冲突时读回原事件。"""
        async with self._pool.acquire() as conn, conn.transaction():
            inserted = await conn.execute(
                f"INSERT INTO {self._events} (tenant_id,event_id,user_id,dispatch_id,event_key,"
                "created_at,acked,record) VALUES($1,$2,$3,$4,$5,$6,FALSE,$7) ON CONFLICT DO NOTHING",
                event.tenant_id, event.event_id, event.user_id, event.dispatch_id, event.key,
                event.created_at, event.model_dump_json())
            if inserted == "INSERT 0 1":
                return event, True
            row = await conn.fetchrow(
                f"SELECT record FROM {self._events} WHERE tenant_id=$1 AND dispatch_id=$2 "
                "AND event_key=$3", event.tenant_id, event.dispatch_id, event.key)
        if row is None:
            raise ConflictError("唤醒事件标识已被占用")
        return ButlerEvent.model_validate_json(row["record"]), False

    async def unacked(self, tenant_id: str, *, limit: int = 50) -> tuple[ButlerEvent, ...]:
        """按写入先后。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._events} WHERE tenant_id=$1 AND NOT acked "
            "ORDER BY created_at,event_id LIMIT $2", tenant_id, limit)
        return tuple(ButlerEvent.model_validate_json(r["record"]) for r in rows)

    async def events_for_dispatch(self, owner: TraceOwner, dispatch_id: str,
                                  ) -> tuple[ButlerEvent, ...]:
        """按创建先后；按员工过滤，他人的看不到。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._events} WHERE tenant_id=$1 AND user_id=$2 "
            "AND dispatch_id=$3 ORDER BY created_at,event_id",
            owner.tenant_id, owner.user_id, dispatch_id)
        return tuple(ButlerEvent.model_validate_json(r["record"]) for r in rows)

    async def decide(self, tenant_id: str, event_id: str, decision: WakeDecision) -> ButlerEvent:
        """锁行；已有决定则保持。"""
        async with self._pool.acquire() as conn, conn.transaction():
            event = await self._locked(conn, tenant_id, event_id)
            if event.decision is not None:
                return event
            updated = event.model_copy(update={"decision": decision})
            await conn.execute(f"UPDATE {self._events} SET record=$3 WHERE tenant_id=$1 AND event_id=$2",
                               tenant_id, event_id, updated.model_dump_json())
        return updated

    async def ack(self, tenant_id: str, event_id: str, at: datetime) -> None:
        """幂等确认。"""
        async with self._pool.acquire() as conn, conn.transaction():
            event = await self._locked(conn, tenant_id, event_id)
            if event.acked_at is not None:
                return
            updated = event.model_copy(update={"acked_at": at})
            await conn.execute(
                f"UPDATE {self._events} SET acked=TRUE,record=$3 WHERE tenant_id=$1 AND event_id=$2",
                tenant_id, event_id, updated.model_dump_json())

    async def _locked(self, conn: Any, tenant_id: str, event_id: str) -> ButlerEvent:
        """锁住并读出一条事件。"""
        row = await conn.fetchrow(
            f"SELECT record FROM {self._events} WHERE tenant_id=$1 AND event_id=$2 FOR UPDATE",
            tenant_id, event_id)
        if row is None:
            raise NotFoundError("唤醒事件不存在")
        return ButlerEvent.model_validate_json(row["record"])

    async def open_pending(self, item: PendingItem) -> tuple[PendingItem, bool]:
        """同键去重。"""
        async with self._pool.acquire() as conn, conn.transaction():
            inserted = await conn.execute(
                f"INSERT INTO {self._pending} (tenant_id,pending_id,user_id,dispatch_id,source_key,"
                "created_at,record) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
                item.tenant_id, item.pending_id, item.user_id, item.dispatch_id, item.source_key,
                item.created_at, item.model_dump_json())
            if inserted == "INSERT 0 1":
                return item, True
            row = await conn.fetchrow(
                f"SELECT record FROM {self._pending} WHERE tenant_id=$1 AND dispatch_id=$2 "
                "AND source_key=$3", item.tenant_id, item.dispatch_id, item.source_key)
        if row is None:
            raise ConflictError("待处理项标识已被占用")
        return PendingItem.model_validate_json(row["record"]), False

    async def list_pending(self, owner: TraceOwner, *, open_only: bool = True,
                           dispatch_id: str | None = None) -> tuple[PendingItem, ...]:
        """本人的项，按创建先后；状态过滤在读出后做（每人的项数量有限）。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._pending} WHERE tenant_id=$1 AND user_id=$2 "
            "AND ($3::text IS NULL OR dispatch_id=$3) ORDER BY created_at,pending_id",
            owner.tenant_id, owner.user_id, dispatch_id)
        items = (PendingItem.model_validate_json(r["record"]) for r in rows)
        return tuple(i for i in items if not open_only or i.state in ("open", "forwarding"))

    async def get_pending(self, owner: TraceOwner, pending_id: str) -> PendingItem:
        """他人的与不存在的一样 404。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._pending} WHERE tenant_id=$1 AND pending_id=$2 AND user_id=$3",
            owner.tenant_id, pending_id, owner.user_id)
        if row is None:
            raise NotFoundError("待处理项不存在")
        return PendingItem.model_validate_json(row["record"])

    async def update_pending(self, owner: TraceOwner, pending_id: str, *, expected_version: int,
                             at: datetime, **fields: Any) -> PendingItem:
        """锁行、核对版本后写回。"""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"SELECT record FROM {self._pending} WHERE tenant_id=$1 AND pending_id=$2 "
                "AND user_id=$3 FOR UPDATE", owner.tenant_id, pending_id, owner.user_id)
            if row is None:
                raise NotFoundError("待处理项不存在")
            item = PendingItem.model_validate_json(row["record"])
            if item.version != expected_version:
                raise ConflictError("待处理项已被更新，请重新读取")
            updated = bump(item, at, fields)
            await conn.execute(
                f"UPDATE {self._pending} SET record=$3 WHERE tenant_id=$1 AND pending_id=$2",
                owner.tenant_id, pending_id, updated.model_dump_json())
        return updated

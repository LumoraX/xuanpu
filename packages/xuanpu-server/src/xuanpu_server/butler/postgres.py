"""派工记录的 PostgreSQL 实现。

建记录在一个事务里先取租户级事务咨询锁，再查幂等、核对容量、插入：同租户的并发派工
串行通过容量核对，不能一起越过上限。改状态锁住该行并核对版本。
拼进 SQL 的只有来自 ``SqlNamespace`` 的表名，取值一律参数绑定。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.butler.dispatch_store import check_capacity, check_owner, replay
from xuanpu_server.butler.models import DispatchRecord, transition
from xuanpu_server.controlplane.schema import DEFAULT_NAMESPACE
from xuanpu_server.errors import ConflictError, NotFoundError

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg
    from taifeng_stores import SqlNamespace

    from xuanpu_server.butler.models import DispatchState
    from xuanpu_server.tracing.models import TraceOwner


class PostgresDispatchStore:
    """与控制面共用连接池与命名空间。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名由命名空间决定。"""
        self._pool = pool
        self._table = namespace.table("butler_dispatch")
        # 咨询锁键带上表名，不同命名空间（测试库）互不阻塞
        self._lock_key = self._table

    async def create(
        self, owner: TraceOwner, record: DispatchRecord, *,
        max_per_trace: int, max_active_traces: int,
    ) -> tuple[DispatchRecord, bool]:
        """租户级咨询锁内：幂等回放 → 容量核对 → 插入。"""
        check_owner(owner, record)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                               f"{self._lock_key}|{record.tenant_id}")
            row = await conn.fetchrow(
                f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3",
                record.tenant_id, record.user_id, record.request_id,
            )
            if row is not None:
                return replay(DispatchRecord.model_validate_json(row["record"]), record), False
            counts = await conn.fetchrow(
                f"SELECT count(*) FILTER (WHERE trace_id=$2) AS in_trace, "
                f"bool_or(active) FILTER (WHERE trace_id=$2) AS trace_active, "
                f"count(DISTINCT trace_id) FILTER (WHERE active) AS active_traces "
                f"FROM {self._table} WHERE tenant_id=$1",
                record.tenant_id, record.trace_id,
            )
            assert counts is not None
            check_capacity(
                in_trace=counts["in_trace"], trace_active=bool(counts["trace_active"]),
                active_traces=counts["active_traces"],
                max_per_trace=max_per_trace, max_active_traces=max_active_traces,
            )
            inserted = await conn.execute(
                f"INSERT INTO {self._table} (tenant_id,dispatch_id,user_id,request_id,trace_id,"
                "active,created_at,record) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING",
                record.tenant_id, record.dispatch_id, record.user_id, record.request_id,
                record.trace_id, record.active, record.created_at, record.model_dump_json(),
            )
            if inserted != "INSERT 0 1":
                raise ConflictError("派工标识已被占用")
        return record, True

    async def get(self, owner: TraceOwner, dispatch_id: str) -> DispatchRecord:
        """他人的与不存在的一样 404。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND dispatch_id=$2 AND user_id=$3",
            owner.tenant_id, dispatch_id, owner.user_id,
        )
        if row is None:
            raise NotFoundError("派工不存在")
        return DispatchRecord.model_validate_json(row["record"])

    async def list_for_trace(self, owner: TraceOwner, trace_id: str) -> tuple[DispatchRecord, ...]:
        """本人在这条 trace 下的派工，按创建先后。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 AND trace_id=$3 "
            "ORDER BY created_at,dispatch_id",
            owner.tenant_id, owner.user_id, trace_id,
        )
        return tuple(DispatchRecord.model_validate_json(row["record"]) for row in rows)

    async def update(
        self, owner: TraceOwner, dispatch_id: str, *, expected_version: int,
        to: DispatchState, at: datetime, host_ref: str | None = None,
        end_reason: str | None = None,
    ) -> DispatchRecord:
        """锁行、核对版本、按状态机推进后写回。"""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND dispatch_id=$2 "
                "AND user_id=$3 FOR UPDATE",
                owner.tenant_id, dispatch_id, owner.user_id,
            )
            if row is None:
                raise NotFoundError("派工不存在")
            current = DispatchRecord.model_validate_json(row["record"])
            if current.version != expected_version:
                raise ConflictError("派工已被更新，请重新读取")
            updated = transition(current, to, at=at, host_ref=host_ref, end_reason=end_reason)
            await conn.execute(
                f"UPDATE {self._table} SET active=$3,record=$4 WHERE tenant_id=$1 AND dispatch_id=$2",
                owner.tenant_id, dispatch_id, updated.active, updated.model_dump_json(),
            )
        return updated

    async def active(self, tenant_id: str, *, limit: int = 200) -> tuple[DispatchRecord, ...]:
        """进行中的派工，按创建先后。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND active "
            "ORDER BY created_at,dispatch_id LIMIT $2", tenant_id, limit)
        return tuple(DispatchRecord.model_validate_json(row["record"]) for row in rows)

    async def find_by_host(self, owner: TraceOwner, host_ref: str) -> DispatchRecord | None:
        """本人承载在 ``host_ref`` 上的派工（按员工过滤后在其派工里查，数量有界）。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 "
            "AND record->>'host_ref'=$3 ORDER BY created_at LIMIT 1",
            owner.tenant_id, owner.user_id, host_ref,
        )
        return None if row is None else DispatchRecord.model_validate_json(row["record"])

    async def active_traces(self, tenant_id: str) -> int:
        """有进行中派工的不同 trace 数。"""
        count = await self._pool.fetchval(
            f"SELECT count(DISTINCT trace_id) FROM {self._table} WHERE tenant_id=$1 AND active",
            tenant_id,
        )
        return int(count)

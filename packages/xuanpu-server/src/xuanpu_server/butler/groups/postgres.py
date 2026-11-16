"""编组记录的 PostgreSQL 实现。保存时锁行并核对版本。

拼进 SQL 的只有来自 ``SqlNamespace`` 的表名，取值一律参数绑定。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.butler.groups.plan import GroupRecord
from xuanpu_server.butler.groups.store import bumped, check_owner, replay
from xuanpu_server.controlplane.schema import DEFAULT_NAMESPACE
from xuanpu_server.errors import ConflictError, NotFoundError

if TYPE_CHECKING:
    import asyncpg
    from taifeng_stores import SqlNamespace

    from xuanpu_server.tracing.models import TraceOwner


class PostgresGroupStore:
    """与控制面共用连接池与命名空间。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名由命名空间决定。"""
        self._pool = pool
        self._table = namespace.table("butler_group")

    async def create(self, owner: TraceOwner, record: GroupRecord) -> tuple[GroupRecord, bool]:
        """插入；同员工同请求标识已存在时按指纹回放或冲突。"""
        check_owner(owner, record)
        async with self._pool.acquire() as conn, conn.transaction():
            inserted = await conn.execute(
                f"INSERT INTO {self._table} (tenant_id,group_id,user_id,request_id,trace_id,"
                "active,created_at,record) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING",
                record.tenant_id, record.group_id, record.user_id, record.request_id,
                record.trace_id, record.active, record.created_at, record.model_dump_json(),
            )
            if inserted == "INSERT 0 1":
                return record, True
            row = await conn.fetchrow(
                f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3",
                record.tenant_id, record.user_id, record.request_id,
            )
        if row is None:
            raise ConflictError("编组标识已被占用")
        return replay(GroupRecord.model_validate_json(row["record"]), record), False

    async def get(self, owner: TraceOwner, group_id: str) -> GroupRecord:
        """他人的与不存在的一样 404。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND group_id=$2 AND user_id=$3",
            owner.tenant_id, group_id, owner.user_id,
        )
        if row is None:
            raise NotFoundError("编组不存在")
        return GroupRecord.model_validate_json(row["record"])

    async def save(self, owner: TraceOwner, record: GroupRecord, *,
                   expected_version: int) -> GroupRecord:
        """锁行、核对版本、整体写回。"""
        check_owner(owner, record)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND group_id=$2 "
                "AND user_id=$3 FOR UPDATE", owner.tenant_id, record.group_id, owner.user_id,
            )
            if row is None:
                raise NotFoundError("编组不存在")
            current = GroupRecord.model_validate_json(row["record"])
            saved = bumped(record, current.version, expected_version)
            await conn.execute(
                f"UPDATE {self._table} SET active=$3,record=$4 WHERE tenant_id=$1 AND group_id=$2",
                owner.tenant_id, record.group_id, saved.active, saved.model_dump_json(),
            )
        return saved

    async def find_by_request(self, owner: TraceOwner, request_id: str) -> GroupRecord | None:
        """本人按请求标识建过的编组。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3",
            owner.tenant_id, owner.user_id, request_id,
        )
        return None if row is None else GroupRecord.model_validate_json(row["record"])

    async def active(self, tenant_id: str, *, limit: int = 200) -> tuple[GroupRecord, ...]:
        """还没收口的编组，按创建先后。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND active "
            "ORDER BY created_at,group_id LIMIT $2", tenant_id, limit,
        )
        return tuple(GroupRecord.model_validate_json(row["record"]) for row in rows)

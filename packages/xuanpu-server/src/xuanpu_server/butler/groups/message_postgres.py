"""会诊消息的 PostgreSQL 实现：同一编组的追加按编组咨询锁串行，序号连续。

拼进 SQL 的只有来自 ``SqlNamespace`` 的表名，取值一律参数绑定。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.butler.groups.messages import GroupMessage, check_message_owner
from xuanpu_server.controlplane.schema import DEFAULT_NAMESPACE

if TYPE_CHECKING:
    import asyncpg
    from taifeng_stores import SqlNamespace

    from xuanpu_server.tracing.models import TraceOwner


class PostgresMessageStore:
    """与控制面共用连接池与命名空间。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名由命名空间决定。"""
        self._pool = pool
        self._table = namespace.table("butler_group_message")

    async def append(self, owner: TraceOwner, message: GroupMessage) -> tuple[GroupMessage, bool]:
        """编组级事务咨询锁内：同键回放，否则取下一个序号插入。"""
        check_message_owner(owner, message)
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                               f"{self._table}|{message.tenant_id}|{message.group_id}")
            row = await conn.fetchrow(
                f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND group_id=$2 AND msg_key=$3",
                message.tenant_id, message.group_id, message.key,
            )
            if row is not None:
                existing = GroupMessage.model_validate_json(row["record"])
                check_message_owner(owner, existing)
                return existing, False
            seq = await conn.fetchval(
                f"SELECT coalesce(max(seq),0)+1 FROM {self._table} WHERE tenant_id=$1 AND group_id=$2",
                message.tenant_id, message.group_id,
            )
            stored = message.model_copy(update={"seq": int(seq)})
            await conn.execute(
                f"INSERT INTO {self._table} (tenant_id,group_id,seq,user_id,msg_key,created_at,record)"
                " VALUES($1,$2,$3,$4,$5,$6,$7)",
                stored.tenant_id, stored.group_id, stored.seq, stored.user_id, stored.key,
                stored.created_at, stored.model_dump_json(),
            )
        return stored, True

    async def list(self, owner: TraceOwner, group_id: str) -> tuple[GroupMessage, ...]:
        """本人的消息，按序号。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND group_id=$2 AND user_id=$3 "
            "ORDER BY seq",
            owner.tenant_id, group_id, owner.user_id,
        )
        return tuple(GroupMessage.model_validate_json(row["record"]) for row in rows)

"""控制面存储 Postgres 实现的审计与用量部分。

由 ``PostgresControlPlane`` 组合使用：持有连接池与表名，``PostgresControlPlane`` 的
审计、用量方法直接委托到这里。每条语句都带 ``tenant_id`` 条件；表名来自命名空间
（字符集已校验），取值一律走参数绑定。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.models import AuditQuery, AuditRecord, UsageRecord

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg

_AUDIT_COLUMNS = (
    "audit_id, tenant_id, source, kind, at, actor, session_id, stream, seq, data::text AS data"
)


def _usage(row: asyncpg.Record) -> UsageRecord:
    """行 → 用量。"""
    return UsageRecord(**dict(row))


def _audit(row: asyncpg.Record) -> AuditRecord:
    """行 → 审计记录。"""
    fields = dict(row)
    fields["data"] = json.loads(fields["data"])
    return AuditRecord(**fields)


class PostgresAuditStore:
    """Postgres 里的审计与用量存储。"""

    def __init__(self, pool: asyncpg.Pool, *, usage_table: str, audit_table: str) -> None:
        """
        Args:
            pool: 宿主创建并管理的连接池（与控制面共用）。
            usage_table: 用量表的完整限定名。
            audit_table: 审计表的完整限定名。
        """
        self._pool = pool
        self._usage_table = usage_table
        self._audit_table = audit_table

    async def _fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        """取多行。"""
        async with self._pool.acquire() as conn:
            return list(await conn.fetch(sql, *args))

    async def _fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        """取一行。"""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    # --- 用量 ---

    async def add_usage(
        self,
        tenant_id: str,
        day: str,
        *,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        turns: int,
    ) -> UsageRecord:
        """累加用量。"""
        table = self._usage_table
        row = await self._fetchrow(
            f"INSERT INTO {table} AS u (tenant_id, day, input_tokens, output_tokens, "
            "total_tokens, turns) VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (tenant_id, day) DO UPDATE SET "
            "input_tokens = u.input_tokens + EXCLUDED.input_tokens, "
            "output_tokens = u.output_tokens + EXCLUDED.output_tokens, "
            "total_tokens = u.total_tokens + EXCLUDED.total_tokens, "
            "turns = u.turns + EXCLUDED.turns RETURNING *",
            tenant_id,
            day,
            input_tokens,
            output_tokens,
            total_tokens,
            turns,
        )
        assert row is not None
        return _usage(row)

    async def get_usage(self, tenant_id: str, day: str) -> UsageRecord:
        """取某一天的用量。"""
        row = await self._fetchrow(
            f"SELECT * FROM {self._usage_table} WHERE tenant_id = $1 AND day = $2", tenant_id, day
        )
        return _usage(row) if row is not None else UsageRecord(tenant_id, day)

    async def list_usage(self, tenant_id: str, *, since: str, until: str) -> list[UsageRecord]:
        """列出一段日期内的用量。"""
        rows = await self._fetch(
            f"SELECT * FROM {self._usage_table} WHERE tenant_id = $1 AND day >= $2 AND day <= $3 "
            "ORDER BY day",
            tenant_id,
            since,
            until,
        )
        return [_usage(row) for row in rows]

    # --- 审计 ---

    async def append_audit(self, records: Sequence[AuditRecord]) -> int:
        """追加审计记录。"""
        if not records:
            return 0
        statement = (
            f"INSERT INTO {self._audit_table} (tenant_id, source, kind, at, actor, session_id, "
            "stream, seq, data) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::text::jsonb) "
            "ON CONFLICT (tenant_id, stream, seq) DO NOTHING"
        )
        written = 0
        async with self._pool.acquire() as conn, conn.transaction():
            for record in records:
                status = await conn.execute(
                    statement,
                    record.tenant_id,
                    record.source,
                    record.kind,
                    record.at,
                    record.actor,
                    record.session_id,
                    record.stream,
                    record.seq,
                    json.dumps(record.data, ensure_ascii=False, default=str),
                )
                written += int(status.rsplit(" ", 1)[-1])
        return written

    async def query_audit(self, tenant_id: str, query: AuditQuery) -> list[AuditRecord]:
        """查询审计记录。"""
        rows = await self._fetch(
            f"SELECT {_AUDIT_COLUMNS} FROM {self._audit_table} "
            "WHERE tenant_id = $1 AND audit_id > $2 "
            "AND ($3::text IS NULL OR session_id = $3) "
            "AND ($4::text IS NULL OR kind = $4) "
            "AND ($5::text IS NULL OR source = $5) "
            "AND ($6::float8 IS NULL OR at >= $6) "
            "AND ($7::float8 IS NULL OR at < $7) "
            "AND ($9::text IS NULL OR data->>'submission_id' = $9) "
            "ORDER BY audit_id LIMIT $8",
            tenant_id,
            query.after_id,
            query.session_id,
            query.kind,
            query.source,
            query.since,
            query.until,
            query.limit,
            query.submission_id,
        )
        return [_audit(row) for row in rows]


__all__ = ["PostgresAuditStore"]

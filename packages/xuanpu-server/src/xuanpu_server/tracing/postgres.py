"""span 存储的 PostgreSQL 实现：与内存实现共用 ``apply_batch``，行为一致。

每次写入是一个事务：先锁 trace 行，再读本批涉及的 span 与 span 总数，应用后整行写回。
拼进 SQL 的只有来自 ``SqlNamespace`` 的表名，取值一律参数绑定。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.schema import DEFAULT_NAMESPACE
from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.tracing.apply import apply_batch
from xuanpu_server.tracing.models import TERMINAL_STATES, SpanRecord, TraceRecord, TraceView
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

    import asyncpg
    from asyncpg.pool import PoolConnectionProxy
    from taifeng_stores import SqlNamespace

    from xuanpu_server.tracing.models import SpanFact, TraceOrigin, TraceOwner

    Connection = asyncpg.Connection | PoolConnectionProxy


class PostgresSpanStore:
    """与控制面共用连接池与命名空间。"""

    def __init__(
        self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE,
        max_spans_per_trace: int = 2048, max_attempts_per_span: int = 64,
    ) -> None:
        """上限由配置注入。"""
        self._pool = pool
        self._trace = namespace.table("trace")
        self._span = namespace.table("trace_span")
        self._max_spans = max_spans_per_trace
        self._max_attempts = max_attempts_per_span

    async def open_trace(
        self, owner: TraceOwner, *, trace_id: str, origin: TraceOrigin, title: str, at: datetime,
    ) -> TraceRecord:
        """幂等建 trace；标识被其他归属占用时报冲突，不泄露对方信息。"""
        record = TraceRecord(
            trace_id=trace_id, owner=owner, origin=origin, title=title,
            created_at=at, updated_at=at,
        )
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"INSERT INTO {self._trace} (tenant_id,trace_id,user_id,created_at,record) "
                "VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                owner.tenant_id, trace_id, owner.user_id, at, record.model_dump_json(),
            )
            row = await conn.fetchrow(
                f"SELECT tenant_id,user_id,record FROM {self._trace} WHERE trace_id=$1", trace_id,
            )
        if row is None or (row["tenant_id"], row["user_id"]) != (owner.tenant_id, owner.user_id):
            raise ConflictError("trace 标识已被占用")
        return TraceRecord.model_validate_json(row["record"])

    async def apply(
        self, owner: TraceOwner, trace_id: str, facts: Sequence[SpanFact], *,
        max_spans: int | None = None, max_attempts: int | None = None,
    ) -> TraceRecord:
        """锁 trace 行后应用本批事实，变化的 span 逐个 upsert。"""
        async with self._pool.acquire() as conn, conn.transaction():
            record = await self._lock(conn, owner, trace_id)
            ids = sorted({fact.span_id for fact in facts})
            rows = await conn.fetch(
                f"SELECT record FROM {self._span} WHERE tenant_id=$1 AND trace_id=$2 "
                "AND span_id=ANY($3::text[])", owner.tenant_id, trace_id, ids,
            )
            known = {span.span_id: span for span in (_span(row) for row in rows)}
            count = await conn.fetchval(
                f"SELECT count(*) FROM {self._span} WHERE tenant_id=$1 AND trace_id=$2",
                owner.tenant_id, trace_id,
            )
            result = apply_batch(
                record, known, int(count), facts,
                max_spans=max_spans or self._max_spans,
                max_attempts=max_attempts or self._max_attempts,
            )
            for span in result.changed.values():
                await self._upsert_span(conn, owner, trace_id, span)
            await self._save(conn, result.record)
            return result.record

    async def mark_incomplete(self, owner: TraceOwner, trace_id: str) -> TraceRecord:
        """显式标不完整。"""
        async with self._pool.acquire() as conn, conn.transaction():
            record = await self._lock(conn, owner, trace_id)
            record = record.model_copy(update={"incomplete": True})
            await self._save(conn, record)
            return record

    async def get(self, owner: TraceOwner, trace_id: str) -> TraceView:
        """返回快照；span 按开始时间与标识排序。"""
        async with self._pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            row = await conn.fetchrow(
                f"SELECT record FROM {self._trace} "
                "WHERE tenant_id=$1 AND user_id=$2 AND trace_id=$3",
                owner.tenant_id, owner.user_id, trace_id,
            )
            if row is None:
                raise NotFoundError("trace 不存在")
            spans = await conn.fetch(
                f"SELECT record FROM {self._span} WHERE tenant_id=$1 AND trace_id=$2 "
                "ORDER BY started_at, span_id", owner.tenant_id, trace_id,
            )
        return TraceView(trace=TraceRecord.model_validate_json(row["record"]),
                         spans=tuple(_span(item) for item in spans))

    async def list(
        self, owner: TraceOwner, *, active: bool | None = None,
        cursor: str | None = None, limit: int = PAGE_DEFAULT,
    ) -> TracePage:
        """按创建时间倒序；总数在过滤后、分页前计算。"""
        check_limit(limit)
        where = "tenant_id=$1 AND user_id=$2 AND ($3::boolean IS NULL OR active=$3)"
        args: list[Any] = [owner.tenant_id, owner.user_id, active]
        async with self._pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            total = await conn.fetchval(f"SELECT count(*) FROM {self._trace} WHERE {where}", *args)
            page_where = where
            if cursor is not None:
                stamp, after_id = decode_cursor(cursor)
                page_where += " AND (created_at,trace_id) < ($4,$5)"
                args += [stamp, after_id]
            args.append(limit + 1)
            rows = await conn.fetch(
                f"SELECT record FROM {self._trace} WHERE {page_where} "
                f"ORDER BY created_at DESC, trace_id DESC LIMIT ${len(args)}", *args,
            )
        items = tuple(TraceRecord.model_validate_json(row["record"]) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit:
            next_cursor = encode_cursor(items[-1].created_at, items[-1].trace_id)
        return TracePage(items=items, next_cursor=next_cursor, total=int(total))

    async def purge(self, tenant_id: str, *, before: datetime, batch: int = 500) -> int:
        """按批删除过期 trace；span 随外键级联删除。"""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                f"DELETE FROM {self._trace} WHERE (tenant_id,trace_id) IN ("
                f"SELECT tenant_id,trace_id FROM {self._trace} "
                "WHERE tenant_id=$1 AND created_at<$2 "
                "ORDER BY created_at LIMIT $3)", tenant_id, before, batch,
            )
        return int(status.rsplit(" ", 1)[1])

    async def _lock(self, conn: Connection, owner: TraceOwner, trace_id: str) -> TraceRecord:
        """锁住本人的 trace 行；不存在与不属于本人同样报 NotFound。"""
        row = await conn.fetchrow(
            f"SELECT record FROM {self._trace} WHERE tenant_id=$1 AND user_id=$2 AND trace_id=$3 "
            "FOR UPDATE", owner.tenant_id, owner.user_id, trace_id,
        )
        if row is None:
            raise NotFoundError("trace 不存在")
        return TraceRecord.model_validate_json(row["record"])

    async def _upsert_span(self, conn: Connection, owner: TraceOwner, trace_id: str,
                           span: SpanRecord) -> None:
        """整行写回一个 span。"""
        await conn.execute(
            f"INSERT INTO {self._span} (tenant_id,trace_id,span_id,started_at,terminal,record) "
            "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(tenant_id,trace_id,span_id) DO UPDATE SET "
            "terminal=EXCLUDED.terminal,record=EXCLUDED.record",
            owner.tenant_id, trace_id, span.span_id, span.started_at,
            span.state in TERMINAL_STATES, span.model_dump_json(),
        )

    async def _save(self, conn: Connection, record: TraceRecord) -> None:
        """写回 trace 头，并按 span 实际状态刷新 active 列。"""
        await conn.execute(
            f"UPDATE {self._trace} SET record=$3, active=EXISTS("
            f"SELECT 1 FROM {self._span} s "
            "WHERE s.tenant_id=$1 AND s.trace_id=$2 AND NOT s.terminal) "
            "WHERE tenant_id=$1 AND trace_id=$2",
            record.owner.tenant_id, record.trace_id, record.model_dump_json(),
        )


def _span(row: asyncpg.Record) -> SpanRecord:
    """数据库行转 span；损坏的 JSON 直接报错，不降级成空。"""
    return SpanRecord.model_validate_json(row["record"])

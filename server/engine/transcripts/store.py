"""跨节点 Transcript 全局元数据索引存储（Memory 与 PostgreSQL，迁移 0037）。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from server.core.controlplane.schema import DEFAULT_NAMESPACE
from server.core.errors import NotFoundError
from server.engine.transcripts.models import TranscriptIndexRecord

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg
    from taifeng_stores import SqlNamespace


class TranscriptIndexStore(Protocol):
    """Transcript 元数据索引契约：强按 ``tenant_id`` 隔离，不存正文。"""

    async def upsert(self, record: TranscriptIndexRecord) -> TranscriptIndexRecord:
        """登记或更新一条 span 的 Transcript 索引。"""
        ...

    async def get(self, tenant_id: str, span_id: str) -> TranscriptIndexRecord:
        """读取指定 span 索引；跨租户或不存在一律 404。"""
        ...

    async def list_for_trace(
        self, tenant_id: str, trace_id: str, *, user_id: str | None = None,
    ) -> tuple[TranscriptIndexRecord, ...]:
        """列出一条 trace 下的全部 span 索引，按创建时间先后。"""
        ...

    async def list_failed(
        self, tenant_id: str, *, user_id: str | None = None, limit: int = 20,
    ) -> tuple[TranscriptIndexRecord, ...]:
        """列出近期失败或中止的 span 索引（供自排查诊断）。"""
        ...

    async def mark_archived(
        self,
        tenant_id: str,
        span_id: str,
        *,
        compact_object_key: str,
        full_object_key: str,
        archived_at: datetime,
    ) -> TranscriptIndexRecord:
        """回填冷归档对象存储键与归档时间。"""
        ...


class MemoryTranscriptIndexStore:
    """进程内 Transcript 索引存储。"""

    def __init__(self) -> None:
        """初始化内存字典与互斥锁。"""
        self._lock = asyncio.Lock()
        self._records: dict[tuple[str, str], TranscriptIndexRecord] = {}

    async def upsert(self, record: TranscriptIndexRecord) -> TranscriptIndexRecord:
        """写入或更新元数据索引。"""
        async with self._lock:
            self._records[(record.tenant_id, record.span_id)] = record
            return record

    async def get(self, tenant_id: str, span_id: str) -> TranscriptIndexRecord:
        """按租户与 span 读取索引。"""
        record = self._records.get((tenant_id, span_id))
        if record is None:
            raise NotFoundError("Transcript 索引不存在")
        return record

    async def list_for_trace(
        self, tenant_id: str, trace_id: str, *, user_id: str | None = None,
    ) -> tuple[TranscriptIndexRecord, ...]:
        """按 trace 过滤并按创建时间升序返回。"""
        found = [
            r for r in self._records.values()
            if r.tenant_id == tenant_id
            and r.trace_id == trace_id
            and (user_id is None or r.user_id == user_id)
        ]
        found.sort(key=lambda r: (r.created_at, r.span_id))
        return tuple(found)

    async def list_failed(
        self, tenant_id: str, *, user_id: str | None = None, limit: int = 20,
    ) -> tuple[TranscriptIndexRecord, ...]:
        """列出失败/中止记录，按时间倒序。"""
        found = [
            r for r in self._records.values()
            if r.tenant_id == tenant_id
            and r.status in ("failed", "cancelled")
            and (user_id is None or r.user_id == user_id)
        ]
        found.sort(key=lambda r: (r.created_at, r.span_id), reverse=True)
        return tuple(found[:limit])

    async def mark_archived(
        self,
        tenant_id: str,
        span_id: str,
        *,
        compact_object_key: str,
        full_object_key: str,
        archived_at: datetime,
    ) -> TranscriptIndexRecord:
        """回填归档对象键。"""
        async with self._lock:
            current = self._records.get((tenant_id, span_id))
            if current is None:
                raise NotFoundError("Transcript 索引不存在")
            updated = current.model_copy(update={
                "compact_object_key": compact_object_key,
                "full_object_key": full_object_key,
                "archived_at": archived_at,
            })
            self._records[(tenant_id, span_id)] = updated
            return updated


class PostgresTranscriptIndexStore:
    """PostgreSQL Transcript 索引存储（迁移 0037）。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名由命名空间派生。"""
        self._pool = pool
        self._table = namespace.table("transcript_index")

    async def upsert(self, record: TranscriptIndexRecord) -> TranscriptIndexRecord:
        """幂等插入或更新索引行。"""
        await self._pool.execute(
            f"INSERT INTO {self._table} (tenant_id,span_id,trace_id,user_id,kind,node_id,"
            "status,created_at,record) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) "
            "ON CONFLICT (tenant_id,span_id) DO UPDATE SET "
            "status=EXCLUDED.status,record=EXCLUDED.record",
            record.tenant_id, record.span_id, record.trace_id, record.user_id, record.kind,
            record.node_id, record.status, record.created_at, record.model_dump_json(),
        )
        return record

    async def get(self, tenant_id: str, span_id: str) -> TranscriptIndexRecord:
        """按租户与 span 读取索引。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND span_id=$2",
            tenant_id, span_id,
        )
        if row is None:
            raise NotFoundError("Transcript 索引不存在")
        return TranscriptIndexRecord.model_validate_json(row["record"])

    async def list_for_trace(
        self, tenant_id: str, trace_id: str, *, user_id: str | None = None,
    ) -> tuple[TranscriptIndexRecord, ...]:
        """按 trace 列出 span 索引。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND trace_id=$2 "
            "AND ($3::text IS NULL OR user_id=$3) ORDER BY created_at, span_id",
            tenant_id, trace_id, user_id,
        )
        return tuple(TranscriptIndexRecord.model_validate_json(r["record"]) for r in rows)

    async def list_failed(
        self, tenant_id: str, *, user_id: str | None = None, limit: int = 20,
    ) -> tuple[TranscriptIndexRecord, ...]:
        """列出近期失败或中止记录。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 "
            "AND status IN ('failed','cancelled') AND ($2::text IS NULL OR user_id=$2) "
            "ORDER BY created_at DESC, span_id DESC LIMIT $3",
            tenant_id, user_id, limit,
        )
        return tuple(TranscriptIndexRecord.model_validate_json(r["record"]) for r in rows)

    async def mark_archived(
        self,
        tenant_id: str,
        span_id: str,
        *,
        compact_object_key: str,
        full_object_key: str,
        archived_at: datetime,
    ) -> TranscriptIndexRecord:
        """事务内回填归档对象键。"""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND span_id=$2 FOR UPDATE",
                tenant_id, span_id,
            )
            if row is None:
                raise NotFoundError("Transcript 索引不存在")
            current = TranscriptIndexRecord.model_validate_json(row["record"])
            updated = current.model_copy(update={
                "compact_object_key": compact_object_key,
                "full_object_key": full_object_key,
                "archived_at": archived_at,
            })
            await conn.execute(
                f"UPDATE {self._table} SET record=$3 WHERE tenant_id=$1 AND span_id=$2",
                tenant_id, span_id, updated.model_dump_json(),
            )
            return updated

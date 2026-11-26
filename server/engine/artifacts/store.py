"""过程产物（Trace Artifact）数据模型与存储实现（Memory / PostgreSQL，迁移 0038）。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime  # noqa: TC003 —— pydantic 运行时解析
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.core.controlplane.schema import DEFAULT_NAMESPACE
from server.core.errors import NotFoundError

if TYPE_CHECKING:
    import asyncpg
    from taifeng_stores import SqlNamespace

    from server.engine.tracing.models import TraceOwner

_SAFE_NAME = re.compile(r"^[^/\\\x00]{1,160}$")


class TraceArtifactRecord(BaseModel):
    """不可变过程产物元数据记录（正本存于共享 ObjectStore）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    artifact_id: str = Field(min_length=1, max_length=80)
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    dispatch_id: str | None = Field(default=None, max_length=128)
    step_index: int | None = Field(default=None, ge=0)
    name: str = Field(min_length=1, max_length=160)
    mime_type: str = Field(min_length=1, max_length=128)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_key: str = Field(min_length=1, max_length=512)
    node_id: str = Field(min_length=1, max_length=128)
    contains_external_input: bool = False
    promoted_item_id: str | None = Field(default=None, max_length=128)
    created_at: datetime

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        """拒绝路径分隔符与特殊目录名。"""
        if not _SAFE_NAME.fullmatch(value) or value in (".", ".."):
            raise ValueError("产物文件名不合法")
        return value


def artifact_view(record: TraceArtifactRecord) -> dict[str, Any]:
    """对外元数据视图，不暴露内部对象存储键。"""
    return {
        "artifact_id": record.artifact_id,
        "trace_id": record.trace_id,
        "dispatch_id": record.dispatch_id,
        "step_index": record.step_index,
        "name": record.name,
        "mime_type": record.mime_type,
        "size": record.size,
        "sha256": record.sha256,
        "contains_external_input": record.contains_external_input,
        "promoted_item_id": record.promoted_item_id,
        "created_at": record.created_at.isoformat(),
    }


class TraceArtifactStore(Protocol):
    """过程产物元数据仓储（按租户与员工强隔离）。"""

    async def put(self, record: TraceArtifactRecord) -> TraceArtifactRecord:
        """登记一条不可变过程产物。"""
        ...

    async def get(self, owner: TraceOwner, artifact_id: str) -> TraceArtifactRecord:
        """读取本人过程产物；他人与不存在一律 404。"""
        ...

    async def list_for_trace(
        self, owner: TraceOwner, trace_id: str,
    ) -> tuple[TraceArtifactRecord, ...]:
        """按创建时间列出本人某条 trace 的全部过程产物。"""
        ...

    async def mark_promoted(
        self, owner: TraceOwner, artifact_id: str, item_id: str,
    ) -> TraceArtifactRecord:
        """记录该产物已转存进个人正式工作台的文件 ID。"""
        ...


class MemoryTraceArtifactStore:
    """进程内过程产物元数据存储。"""

    def __init__(self) -> None:
        """初始化内存字典与互斥锁。"""
        self._lock = asyncio.Lock()
        self._records: dict[tuple[str, str], TraceArtifactRecord] = {}

    async def put(self, record: TraceArtifactRecord) -> TraceArtifactRecord:
        """写入一条产物索引。"""
        async with self._lock:
            self._records[(record.tenant_id, record.artifact_id)] = record
            return record

    async def get(self, owner: TraceOwner, artifact_id: str) -> TraceArtifactRecord:
        """按归属读取产物。"""
        rec = self._records.get((owner.tenant_id, artifact_id))
        if rec is None or rec.user_id != owner.user_id:
            raise NotFoundError("过程产物不存在")
        return rec

    async def list_for_trace(
        self, owner: TraceOwner, trace_id: str,
    ) -> tuple[TraceArtifactRecord, ...]:
        """按创建时间先后返回本人 trace 产物。"""
        found = [
            r for r in self._records.values()
            if r.tenant_id == owner.tenant_id
            and r.user_id == owner.user_id
            and r.trace_id == trace_id
        ]
        found.sort(key=lambda r: (r.created_at, r.artifact_id))
        return tuple(found)

    async def mark_promoted(
        self, owner: TraceOwner, artifact_id: str, item_id: str,
    ) -> TraceArtifactRecord:
        """更新已转存正式文件 ID。"""
        async with self._lock:
            rec = self._records.get((owner.tenant_id, artifact_id))
            if rec is None or rec.user_id != owner.user_id:
                raise NotFoundError("过程产物不存在")
            updated = rec.model_copy(update={"promoted_item_id": item_id})
            self._records[(owner.tenant_id, artifact_id)] = updated
            return updated


class PostgresTraceArtifactStore:
    """PostgreSQL 过程产物元数据存储（迁移 0038）。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名由命名空间派生。"""
        self._pool = pool
        self._table = namespace.table("trace_artifact")

    async def put(self, record: TraceArtifactRecord) -> TraceArtifactRecord:
        """幂等写入一条产物元数据。"""
        await self._pool.execute(
            f"INSERT INTO {self._table} (tenant_id,artifact_id,user_id,trace_id,dispatch_id,"
            "created_at,record) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
            record.tenant_id, record.artifact_id, record.user_id, record.trace_id,
            record.dispatch_id, record.created_at, record.model_dump_json(),
        )
        return record

    async def get(self, owner: TraceOwner, artifact_id: str) -> TraceArtifactRecord:
        """按归属读取产物。"""
        row = await self._pool.fetchrow(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 AND artifact_id=$3",
            owner.tenant_id, owner.user_id, artifact_id,
        )
        if row is None:
            raise NotFoundError("过程产物不存在")
        return TraceArtifactRecord.model_validate_json(row["record"])

    async def list_for_trace(
        self, owner: TraceOwner, trace_id: str,
    ) -> tuple[TraceArtifactRecord, ...]:
        """列出本人某条 trace 下的产物。"""
        rows = await self._pool.fetch(
            f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 "
            "AND trace_id=$3 ORDER BY created_at, artifact_id",
            owner.tenant_id, owner.user_id, trace_id,
        )
        return tuple(TraceArtifactRecord.model_validate_json(r["record"]) for r in rows)

    async def mark_promoted(
        self, owner: TraceOwner, artifact_id: str, item_id: str,
    ) -> TraceArtifactRecord:
        """事务内更新转存后的个人空间文件 ID。"""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND user_id=$2 "
                "AND artifact_id=$3 FOR UPDATE",
                owner.tenant_id, owner.user_id, artifact_id,
            )
            if row is None:
                raise NotFoundError("过程产物不存在")
            rec = TraceArtifactRecord.model_validate_json(row["record"])
            updated = rec.model_copy(update={"promoted_item_id": item_id})
            await conn.execute(
                f"UPDATE {self._table} SET record=$4 WHERE tenant_id=$1 AND user_id=$2 AND artifact_id=$3",
                owner.tenant_id, owner.user_id, artifact_id, updated.model_dump_json(),
            )
            return updated

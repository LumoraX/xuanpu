"""知识问答上下文的记录与存储（R4-D1）：内存与 PostgreSQL 同一契约。

上下文固定一组命中（来源、序号、来源代次与 SHA），回答缓存只附在上下文里；
来源失效时上下文标 ``blocked`` 并清掉回答正文，只留不含正文的计数。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from server.core.errors import ConflictError

if TYPE_CHECKING:
    import asyncpg
    from taifeng_stores import SqlNamespace


class Pin(BaseModel):
    """固定的一段依据：来源、序号、登记时的代次与 SHA（用于判定此后是否仍是同一份内容）。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    source_id: str
    ordinal: int
    source_epoch: int
    sha256: str
    item_id: str | None = None
    revision: int
    title: str


class Answer(BaseModel):
    """一次问答的缓存：问题、回答原文、引用到的依据编号、是否有依据。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    id: str
    request_id: str
    question: str
    text: str
    citations: tuple[int, ...] = ()
    grounded: bool
    created_at: float


class KnowledgeContext(BaseModel):
    """本人一个受保护的问答上下文。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    tenant_id: str
    owner: str
    id: str
    request_id: str
    pins: tuple[Pin, ...]
    status: Literal['active', 'blocked', 'closed'] = 'active'
    block_reason: str | None = None
    answers: tuple[Answer, ...] = ()
    purged_answers: int = 0
    version: int = 1
    created_at: float
    updated_at: float


class ContextStore(Protocol):
    """上下文的持久化。"""

    async def put(self, context: KnowledgeContext, *, expected_version: int | None) -> KnowledgeContext:
        """新增（expected_version 为 None）或按版本改写。"""
        ...

    async def list(self, tenant: str, owner: str) -> list[KnowledgeContext]:
        """本人全部上下文（按创建顺序）。"""
        ...


class MemoryContextStore:
    """内存实现。"""

    def __init__(self) -> None:
        """空表。"""
        self._rows: dict[tuple[str, str, str], KnowledgeContext] = {}
        self._lock = asyncio.Lock()

    async def put(self, context: KnowledgeContext, *, expected_version: int | None) -> KnowledgeContext:
        """CAS 写入。"""
        key = (context.tenant_id, context.owner, context.id)
        async with self._lock:
            current = self._rows.get(key)
            if (current is None) != (expected_version is None) or (
                    current is not None and current.version != expected_version):
                raise ConflictError('问答上下文已变化，请刷新后重试')
            self._rows[key] = context
            return context

    async def list(self, tenant: str, owner: str) -> list[KnowledgeContext]:
        """本人上下文。"""
        return sorted((c for (t, o, _), c in self._rows.items() if (t, o) == (tenant, owner)),
                      key=lambda c: (c.created_at, c.id))


class PostgresContextStore:
    """PostgreSQL 实现：表 ``knowledge_context``（迁移 0032）。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace) -> None:
        """连接池与命名空间由控制面注入。"""
        self._pool = pool
        self._table = namespace.table('knowledge_context')

    async def put(self, context: KnowledgeContext, *, expected_version: int | None) -> KnowledgeContext:
        """新增用主键冲突挡重复，改写用版本条件（回答正文随记录一起覆盖清除）。"""
        async with self._pool.acquire() as conn:
            if expected_version is None:
                done = await conn.execute(
                    f'INSERT INTO {self._table} (tenant_id,owner,id,record) VALUES($1,$2,$3,$4) '
                    'ON CONFLICT DO NOTHING', context.tenant_id, context.owner, context.id,
                    context.model_dump_json())
            else:
                done = await conn.execute(
                    f"UPDATE {self._table} SET record=$4 WHERE tenant_id=$1 AND owner=$2 AND id=$3 "
                    "AND (record->>'version')::int=$5",
                    context.tenant_id, context.owner, context.id, context.model_dump_json(), expected_version)
        if done.split()[-1] != '1':
            raise ConflictError('问答上下文已变化，请刷新后重试')
        return context

    async def list(self, tenant: str, owner: str) -> list[KnowledgeContext]:
        """本人上下文。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f'SELECT record FROM {self._table} WHERE tenant_id=$1 AND owner=$2',
                                    tenant, owner)
        found = [KnowledgeContext.model_validate_json(r['record']) for r in rows]
        return sorted(found, key=lambda c: (c.created_at, c.id))


class PinInput(BaseModel):
    """员工从检索结果里选定的一段：来源与序号（代次与 SHA 由服务端填）。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    source_id: str = Field(min_length=1, max_length=64)
    ordinal: int = Field(ge=0)


class CreateContext(BaseModel):
    """建上下文：操作号与 1–10 段选定依据。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    pins: list[PinInput] = Field(min_length=1, max_length=10)


class AskInput(BaseModel):
    """在上下文里提问：操作号与 1–500 字问题。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=500, pattern=r'\S')


class ContextChange(BaseModel):
    """结束上下文须带当前版本。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    expected_version: int = Field(ge=1)

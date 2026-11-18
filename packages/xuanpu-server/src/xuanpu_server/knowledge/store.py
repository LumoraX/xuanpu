"""知识来源与文本块的存储（R4-D0）：内存与 PostgreSQL 同一契约。

来源属于员工本人；所有读写都带租户与员工，不提供跨员工或全库枚举。文本块是派生数据，
随来源一起写入、一起逻辑删除；检索按字面匹配（不拼 SQL 通配符），只在调用方给出的来源集合里找。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.errors import ConflictError

if TYPE_CHECKING:
    import asyncpg
    from taifeng_stores import SqlNamespace


class SourceRef(BaseModel):
    """本人工作区文件的精确版本（共享资料包来源待后续版本）。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    kind: Literal['workspace_file']
    item_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)


class KnowledgeSource(BaseModel):
    """一个已登记的来源：固定版本与 SHA、提取器版本、状态与代次。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    tenant_id: str
    owner: str
    id: str
    ref: SourceRef
    title: str
    sha256: str
    mime_type: str
    extractor: str
    chars: int
    chunks: int
    status: Literal['ready', 'stale', 'deleted'] = 'ready'
    epoch: int = 1
    version: int = 1
    created_at: float
    updated_at: float


class Chunk(BaseModel):
    """来源里的一段文本（有序号，可回指来源与版本）。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    source_id: str
    ordinal: int
    text: str


class KnowledgeStore(Protocol):
    """来源与文本块的持久化。"""

    async def add(self, source: KnowledgeSource, chunks: list[Chunk]) -> KnowledgeSource:
        """登记来源并写入文本块（同一员工同一来源版本已存在则 409）。"""
        ...

    async def sources(self, tenant: str, owner: str) -> list[KnowledgeSource]:
        """本人全部来源（含已删除，按创建顺序）。"""
        ...

    async def update(self, source: KnowledgeSource, *, expected_version: int) -> KnowledgeSource:
        """按版本改状态；删除时同时清掉文本块。"""
        ...

    async def match(self, tenant: str, owner: str, source_ids: list[str], query: str,
                    limit: int) -> tuple[list[Chunk], int]:
        """在给定来源里按字面（不区分大小写）找文本块：返回前 ``limit`` 条与匹配总数。"""
        ...


class MemoryKnowledgeStore:
    """内存实现。"""

    def __init__(self) -> None:
        """空表。"""
        self._sources: dict[tuple[str, str, str], KnowledgeSource] = {}
        self._chunks: dict[tuple[str, str], list[Chunk]] = {}
        self._lock = asyncio.Lock()

    async def add(self, source: KnowledgeSource, chunks: list[Chunk]) -> KnowledgeSource:
        """同一员工同一来源版本只登记一次（未删除时）。"""
        async with self._lock:
            for item in self._sources.values():
                if ((item.tenant_id, item.owner, item.ref) == (source.tenant_id, source.owner, source.ref)
                        and item.status != 'deleted'):
                    raise ConflictError('这份材料的这个版本已经登记过')
            self._sources[(source.tenant_id, source.owner, source.id)] = source
            self._chunks[(source.tenant_id, source.id)] = list(chunks)
            return source

    async def sources(self, tenant: str, owner: str) -> list[KnowledgeSource]:
        """本人来源。"""
        return sorted((s for (t, o, _), s in self._sources.items() if (t, o) == (tenant, owner)),
                      key=lambda s: (s.created_at, s.id))

    async def update(self, source: KnowledgeSource, *, expected_version: int) -> KnowledgeSource:
        """CAS 改状态；删除清文本块。"""
        async with self._lock:
            key = (source.tenant_id, source.owner, source.id)
            current = self._sources.get(key)
            if current is None or current.version != expected_version:
                raise ConflictError('来源已变化，请刷新后重试')
            self._sources[key] = source
            if source.status == 'deleted':
                self._chunks.pop((source.tenant_id, source.id), None)
            return source

    async def match(self, tenant: str, owner: str, source_ids: list[str], query: str,
                    limit: int) -> tuple[list[Chunk], int]:
        """字面匹配，按来源与序号排序。"""
        needle = query.casefold()
        found = [c for sid in sorted(source_ids) for c in self._chunks.get((tenant, sid), [])
                 if needle in c.text.casefold()
                 and (tenant, owner, sid) in self._sources]
        return found[:limit], len(found)


class PostgresKnowledgeStore:
    """PostgreSQL 实现：表 ``knowledge_source``、``knowledge_chunk``（迁移 0029）。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace) -> None:
        """连接池与命名空间由控制面注入。"""
        self._pool = pool
        self._sources = namespace.table('knowledge_source')
        self._chunks = namespace.table('knowledge_chunk')

    async def add(self, source: KnowledgeSource, chunks: list[Chunk]) -> KnowledgeSource:
        """来源与全部文本块同一事务写入；唯一约束挡住同版本重复登记。"""
        async with self._pool.acquire() as conn, conn.transaction():
            live = await conn.fetchval(
                f"SELECT count(*) FROM {self._sources} WHERE tenant_id=$1 AND owner=$2 AND ref=$3 "
                "AND record->>'status'<>'deleted'", source.tenant_id, source.owner, source.ref.model_dump_json())
            if live:
                raise ConflictError('这份材料的这个版本已经登记过')
            await conn.execute(
                f'INSERT INTO {self._sources} (tenant_id,owner,id,ref,record) VALUES($1,$2,$3,$4,$5)',
                source.tenant_id, source.owner, source.id, source.ref.model_dump_json(), source.model_dump_json())
            await conn.executemany(
                f'INSERT INTO {self._chunks} (tenant_id,owner,source_id,ordinal,text) VALUES($1,$2,$3,$4,$5)',
                [(source.tenant_id, source.owner, c.source_id, c.ordinal, c.text) for c in chunks])
        return source

    async def sources(self, tenant: str, owner: str) -> list[KnowledgeSource]:
        """本人来源。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f'SELECT record FROM {self._sources} WHERE tenant_id=$1 AND owner=$2', tenant, owner)
        found = [KnowledgeSource.model_validate_json(r['record']) for r in rows]
        return sorted(found, key=lambda s: (s.created_at, s.id))

    async def update(self, source: KnowledgeSource, *, expected_version: int) -> KnowledgeSource:
        """按版本改状态；删除时同事务清文本块。"""
        async with self._pool.acquire() as conn, conn.transaction():
            done = await conn.execute(
                f"UPDATE {self._sources} SET record=$4 WHERE tenant_id=$1 AND owner=$2 AND id=$3 "
                "AND (record->>'version')::int=$5",
                source.tenant_id, source.owner, source.id, source.model_dump_json(), expected_version)
            if done.split()[-1] != '1':
                raise ConflictError('来源已变化，请刷新后重试')
            if source.status == 'deleted':
                await conn.execute(f'DELETE FROM {self._chunks} WHERE tenant_id=$1 AND source_id=$2',
                                   source.tenant_id, source.id)
        return source

    async def match(self, tenant: str, owner: str, source_ids: list[str], query: str,
                    limit: int) -> tuple[list[Chunk], int]:
        """``strpos`` 字面匹配（不经 LIKE，%、_ 就是字符本身），只在给定来源里找。"""
        if not source_ids:
            return [], 0
        where = ('WHERE tenant_id=$1 AND owner=$2 AND source_id=ANY($3::text[]) '
                 'AND strpos(lower(text), lower($4)) > 0')
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(f'SELECT count(*) FROM {self._chunks} {where}',
                                        tenant, owner, source_ids, query)
            rows = await conn.fetch(
                f'SELECT source_id,ordinal,text FROM {self._chunks} {where} ORDER BY source_id,ordinal LIMIT $5',
                tenant, owner, source_ids, query, limit)
        return [Chunk(source_id=r['source_id'], ordinal=r['ordinal'], text=r['text']) for r in rows], int(total)

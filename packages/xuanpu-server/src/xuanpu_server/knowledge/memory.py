"""本人工作记忆（R4-D2）：只有本人确认才成为记忆，能更正、能删除，不共享、不授权。

- 记忆类型只有两种：``confirmed_fact``（由模型或本人提出、引用本人此刻可读的知识来源、经本人确认）
  与 ``user_assertion``（本人手工录入，标明是本人陈述而非已核实）；
- 模型只能提出候选（``proposed``），不能静默确认；候选须带来源引用，确认时再核一次来源仍可读；
- 更正生成新修订、旧修订标 superseded；删除与过期使记忆失效（代次加一），之后不再出现在列表或回答里；
- 不自动共享给同事或岗位，不据此授予任何能力或审批权。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.ids import new_id

if TYPE_CHECKING:
    import asyncpg
    from pydantic import JsonValue
    from taifeng_stores import SqlNamespace

    from xuanpu_server.tenancy import Principal

    from .service import KnowledgeService

MAX_MEMORIES = 500


class MemoryCitation(BaseModel):
    """记忆的依据：本人知识来源的一段（来源、版本、位置）。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    source_id: str = Field(min_length=1, max_length=64)
    ordinal: int = Field(ge=0)


class WorkMemory(BaseModel):
    """一条记忆的一个修订。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    tenant_id: str
    owner: str
    id: str
    lineage: str
    revision: int = 1
    kind: Literal['confirmed_fact', 'user_assertion']
    text: str
    citation: MemoryCitation | None = None
    valid_until: float | None = None
    status: Literal['proposed', 'active', 'rejected', 'superseded', 'deleted'] = 'proposed'
    version: int = 1
    created_at: float
    updated_at: float


class ProposeInput(BaseModel):
    """提出候选：原文与本人可读来源的引用（模型提出时必须带引用）。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=1000, pattern=r'\S')
    citation: MemoryCitation
    valid_until: float | None = Field(default=None, gt=0)


class AssertInput(BaseModel):
    """本人手工录入的一条陈述（不标为已核实）。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=1000, pattern=r'\S')
    valid_until: float | None = Field(default=None, gt=0)


class DecideInput(BaseModel):
    """确认、拒绝或删除须带当前版本。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    expected_version: int = Field(ge=1)


class CorrectInput(DecideInput):
    """更正：新的原文（依据沿用原记忆）。"""

    text: str = Field(min_length=1, max_length=1000, pattern=r'\S')


class MemoryStore(Protocol):
    """记忆修订的持久化。"""

    async def put(self, memory: WorkMemory, *, expected_version: int | None) -> WorkMemory:
        """新增（expected_version 为 None）或按版本改写一个修订。"""
        ...

    async def list(self, tenant: str, owner: str) -> list[WorkMemory]:
        """本人全部修订。"""
        ...


class MemoryMemoryStore:
    """内存实现。"""

    def __init__(self) -> None:
        """空表。"""
        self._rows: dict[tuple[str, str, str], WorkMemory] = {}
        self._lock = asyncio.Lock()

    async def put(self, memory: WorkMemory, *, expected_version: int | None) -> WorkMemory:
        """CAS 写入。"""
        key = (memory.tenant_id, memory.owner, memory.id)
        async with self._lock:
            current = self._rows.get(key)
            if (current is None) != (expected_version is None) or (
                    current is not None and current.version != expected_version):
                raise ConflictError('记忆已变化，请刷新后重试')
            self._rows[key] = memory
            return memory

    async def list(self, tenant: str, owner: str) -> list[WorkMemory]:
        """本人修订（按创建顺序）。"""
        return sorted((m for (t, o, _), m in self._rows.items() if (t, o) == (tenant, owner)),
                      key=lambda m: (m.created_at, m.id))


class PostgresMemoryStore:
    """PostgreSQL 实现：表 ``work_memory``（迁移 0030）。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace) -> None:
        """连接池与命名空间由控制面注入。"""
        self._pool = pool
        self._table = namespace.table('work_memory')

    async def put(self, memory: WorkMemory, *, expected_version: int | None) -> WorkMemory:
        """新增用主键冲突挡重复，改写用版本条件。"""
        async with self._pool.acquire() as conn:
            if expected_version is None:
                done = await conn.execute(
                    f'INSERT INTO {self._table} (tenant_id,owner,id,record) VALUES($1,$2,$3,$4) '
                    'ON CONFLICT DO NOTHING', memory.tenant_id, memory.owner, memory.id, memory.model_dump_json())
            else:
                done = await conn.execute(
                    f"UPDATE {self._table} SET record=$4 WHERE tenant_id=$1 AND owner=$2 AND id=$3 "
                    "AND (record->>'version')::int=$5",
                    memory.tenant_id, memory.owner, memory.id, memory.model_dump_json(), expected_version)
        if done.split()[-1] != '1':
            raise ConflictError('记忆已变化，请刷新后重试')
        return memory

    async def list(self, tenant: str, owner: str) -> list[WorkMemory]:
        """本人修订。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f'SELECT record FROM {self._table} WHERE tenant_id=$1 AND owner=$2',
                                    tenant, owner)
        found = [WorkMemory.model_validate_json(r['record']) for r in rows]
        return sorted(found, key=lambda m: (m.created_at, m.id))


def memory_view(memory: WorkMemory) -> dict[str, JsonValue]:
    """记忆视图：原文、类型（是否本人陈述）、依据、期限与状态。"""
    return {'id': memory.id, 'lineage': memory.lineage, 'revision': memory.revision, 'kind': memory.kind,
            'text': memory.text, 'citation': memory.citation.model_dump(mode='json') if memory.citation else None,
            'valid_until': memory.valid_until, 'status': memory.status, 'version': memory.version,
            'verified_by_source': memory.kind == 'confirmed_fact', 'created_at': memory.created_at}


class WorkMemoryService:
    """本人工作记忆服务（与知识来源同一开关）。"""

    def __init__(self, knowledge: KnowledgeService, store: MemoryStore) -> None:
        """复用知识服务的员工核对、来源可读判定与时钟。"""
        self.knowledge, self.store = knowledge, store

    async def _cited(self, who: Principal, citation: MemoryCitation) -> None:
        """引用须指向本人此刻可读的来源里存在的一段。"""
        live = {s.id: s for s, _ in await self.knowledge._live(who)}
        source = live.get(citation.source_id)
        if source is None or citation.ordinal >= source.chunks:
            raise InvalidRequestError('引用的来源不可读或不存在')

    async def _get(self, who: Principal, memory_id: str) -> WorkMemory:
        """本人的一条修订；别人的或不存在 404。"""
        tenant, user = await self.knowledge._employee(who)
        found = next((m for m in await self.store.list(tenant, user) if m.id == memory_id), None)
        if found is None:
            raise NotFoundError('记忆不存在')
        return found

    async def _new(self, who: Principal, kind: Literal['confirmed_fact', 'user_assertion'], text: str,
                   citation: MemoryCitation | None, valid_until: float | None, status: Literal['proposed', 'active'],
                   lineage: str | None = None, revision: int = 1) -> WorkMemory:
        """写一个新修订（数量有上限）。"""
        tenant, user = await self.knowledge._employee(who)
        if len(await self.store.list(tenant, user)) >= MAX_MEMORIES:
            raise ConflictError('工作记忆数量已达上限')
        now = self.knowledge.now()
        identifier = new_id('wm')
        memory = WorkMemory(tenant_id=tenant, owner=user, id=identifier, lineage=lineage or identifier,
                            revision=revision, kind=kind, text=text, citation=citation, valid_until=valid_until,
                            status=status, created_at=now, updated_at=now)
        return await self.store.put(memory, expected_version=None)

    async def propose(self, who: Principal, body: ProposeInput) -> dict[str, JsonValue]:
        """提出候选（不生效，等本人确认）。"""
        await self._cited(who, body.citation)
        return memory_view(await self._new(who, 'confirmed_fact', body.text, body.citation, body.valid_until,
                                           'proposed'))

    async def assert_fact(self, who: Principal, body: AssertInput) -> dict[str, JsonValue]:
        """本人手工录入：直接生效，但标明是本人陈述。"""
        return memory_view(await self._new(who, 'user_assertion', body.text, None, body.valid_until, 'active'))

    async def _change(self, who: Principal, memory_id: str, expected: int, status: str,
                      allowed: tuple[str, ...]) -> WorkMemory:
        """按版本改状态。"""
        memory = await self._get(who, memory_id)
        if memory.status not in allowed:
            raise ConflictError('这条记忆当前状态不允许这个操作')
        changed = memory.model_copy(update={'status': status, 'version': memory.version + 1,
                                            'updated_at': self.knowledge.now()})
        return await self.store.put(changed, expected_version=expected)

    async def confirm(self, who: Principal, memory_id: str, body: DecideInput) -> dict[str, JsonValue]:
        """本人确认候选：确认时再核一次来源仍可读。"""
        memory = await self._get(who, memory_id)
        if memory.citation is not None:
            await self._cited(who, memory.citation)
        return memory_view(await self._change(who, memory_id, body.expected_version, 'active', ('proposed',)))

    async def reject(self, who: Principal, memory_id: str, body: DecideInput) -> dict[str, JsonValue]:
        """本人拒绝候选。"""
        return memory_view(await self._change(who, memory_id, body.expected_version, 'rejected', ('proposed',)))

    async def delete(self, who: Principal, memory_id: str, body: DecideInput) -> dict[str, JsonValue]:
        """删除一条记忆（之后不再出现）。"""
        return memory_view(await self._change(who, memory_id, body.expected_version, 'deleted',
                                              ('active', 'proposed')))

    async def correct(self, who: Principal, memory_id: str, body: CorrectInput) -> dict[str, JsonValue]:
        """更正：旧修订标 superseded，生成新修订（类型与依据沿用）。"""
        memory = await self._get(who, memory_id)
        if memory.status != 'active':
            raise ConflictError('只能更正生效中的记忆')
        await self._change(who, memory_id, body.expected_version, 'superseded', ('active',))
        fresh = await self._new(who, memory.kind, body.text, memory.citation, memory.valid_until, 'active',
                                lineage=memory.lineage, revision=memory.revision + 1)
        return memory_view(fresh)

    async def listing(self, who: Principal) -> dict[str, JsonValue]:
        """本人生效中与待确认的记忆（过期的不列，依据不可读的确认事实不列）。"""
        tenant, user = await self.knowledge._employee(who)
        now = self.knowledge.now()
        live = {s.id for s, _ in await self.knowledge._live(who)}
        items: list[JsonValue] = []
        for memory in await self.store.list(tenant, user):
            if memory.status not in ('active', 'proposed'):
                continue
            if memory.valid_until is not None and memory.valid_until <= now:
                continue
            if memory.citation is not None and memory.citation.source_id not in live:
                continue
            items.append(memory_view(memory))
        return {'memories': items, 'total': len(items)}

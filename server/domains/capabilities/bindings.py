"""调用绑定（R4-B1）：一次新受理（会话或任务）固定下来的能力依据。

绑定只写一次（同键重复写入须完全相同），之后每次续聊、恢复、批准后继续都拿当前资格对照它：
不用「现在恰好有别的授权」替换失效的原依据。单独成表，不进旧会话/任务记录的严格 JSON。
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from server.core.errors import ConflictError

if TYPE_CHECKING:
    import asyncpg
    from taifeng_stores import SqlNamespace

BindingKind = Literal['session', 'task']


class InvocationBinding(BaseModel):
    """绑定记录：受理对象、员工与能力决策的全部字段。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    tenant_id: str
    kind: BindingKind
    ref_id: str
    user_id: str
    user_epoch: int
    capability_id: str
    capability_revision: int
    package_name: str
    package_version: str
    package_digest: str
    entry: str
    installation_id: str
    installation_version: int
    config_id: str
    config_revision: int
    config_digest: str
    assignment_id: str | None = None
    assignment_epoch: int | None = None
    release_id: str | None = None
    release_epoch: int | None = None
    evaluation_id: str | None = None
    created_at: float


class BindingStore(Protocol):
    """绑定存储：一次写入、按受理对象读取。"""

    async def put(self, binding: InvocationBinding) -> InvocationBinding:
        """写入；同键已存在且内容不同 409，相同则回原记录。"""
        ...

    async def get(self, tenant_id: str, kind: BindingKind, ref_id: str) -> InvocationBinding | None:
        """读取；没有返回 None。"""
        ...

    async def tasks(self, tenant_id: str, user_id: str | None = None) -> list[InvocationBinding]:
        """任务绑定（可按员工过滤），按创建时间与对象排序；只给归因统计用。"""
        ...


class MemoryBindingStore:
    """内存实现（试用与测试）。"""

    def __init__(self) -> None:
        """空表。"""
        self._rows: dict[tuple[str, str, str], InvocationBinding] = {}
        self._lock = asyncio.Lock()

    async def put(self, binding: InvocationBinding) -> InvocationBinding:
        """同键只写一次。"""
        key = (binding.tenant_id, binding.kind, binding.ref_id)
        async with self._lock:
            existing = self._rows.get(key)
            if existing is not None:
                if existing.model_dump(exclude={'created_at'}) != binding.model_dump(exclude={'created_at'}):
                    raise ConflictError('受理对象已绑定其他能力依据')
                return existing
            self._rows[key] = binding
            return binding

    async def get(self, tenant_id: str, kind: BindingKind, ref_id: str) -> InvocationBinding | None:
        """按受理对象读。"""
        return self._rows.get((tenant_id, kind, ref_id))

    async def tasks(self, tenant_id: str, user_id: str | None = None) -> list[InvocationBinding]:
        """任务绑定。"""
        found = [b for (t, k, _), b in self._rows.items() if t == tenant_id and k == 'task'
                 and (user_id is None or b.user_id == user_id)]
        return sorted(found, key=lambda b: (b.created_at, b.ref_id))


class PostgresBindingStore:
    """PostgreSQL 实现：表 ``capability_binding``（迁移 0028），按租户与受理对象主键。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace) -> None:
        """连接池与命名空间由控制面注入。"""
        self._pool = pool
        self._table = namespace.table('capability_binding')

    async def put(self, binding: InvocationBinding) -> InvocationBinding:
        """插入；冲突时读回比较，不同则 409。"""
        async with self._pool.acquire() as conn:
            await conn.execute(
                f'INSERT INTO {self._table} (tenant_id,kind,ref_id,user_id,capability_id,record) '
                'VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT (tenant_id,kind,ref_id) DO NOTHING',
                binding.tenant_id, binding.kind, binding.ref_id, binding.user_id,
                binding.capability_id, binding.model_dump_json())
        existing = await self.get(binding.tenant_id, binding.kind, binding.ref_id)
        assert existing is not None
        if existing.model_dump(exclude={'created_at'}) != binding.model_dump(exclude={'created_at'}):
            raise ConflictError('受理对象已绑定其他能力依据')
        return existing

    async def get(self, tenant_id: str, kind: BindingKind, ref_id: str) -> InvocationBinding | None:
        """按主键读。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f'SELECT record FROM {self._table} WHERE tenant_id=$1 AND kind=$2 AND ref_id=$3',
                tenant_id, kind, ref_id)
        return InvocationBinding.model_validate_json(row['record']) if row is not None else None

    async def tasks(self, tenant_id: str, user_id: str | None = None) -> list[InvocationBinding]:
        """任务绑定（按员工过滤时走 user_id 列）。"""
        query = f"SELECT record FROM {self._table} WHERE tenant_id=$1 AND kind='task'"
        args: list[object] = [tenant_id]
        if user_id is not None:
            query += ' AND user_id=$2'
            args.append(user_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        found = [InvocationBinding.model_validate_json(r['record']) for r in rows]
        return sorted(found, key=lambda b: (b.created_at, b.ref_id))

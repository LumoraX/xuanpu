"""PG协调事务固定锁序并原子提交组织、回执、事件及确认。"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from xuanpu_server.collaboration.locks import purpose_participants
from xuanpu_server.collaboration.postgres import load_collaboration, save_collaboration
from xuanpu_server.collaboration.store import validate_collaboration
from xuanpu_server.coordination.models import CoordinationState, CoordinationTransaction
from xuanpu_server.coordination.outbox import CoordinationEvent
from xuanpu_server.coordination.receipts import CoordinationReceipt, receipt_key
from xuanpu_server.coordination.validation import validate_commit
from xuanpu_server.errors import InvalidRequestError, NotFoundError, PermissionDeniedError
from xuanpu_server.organization.postgres import load_organization, save_organization
from xuanpu_server.organization.user_profiles import save_profiles, validate_profiles
from xuanpu_server.shared_matters.postgres import load_shared, save_shared
from xuanpu_server.shared_matters.store import validate_shared
from xuanpu_server.transaction_reads import read_connection, transaction_reads

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg
    from asyncpg.pool import PoolConnectionProxy
    from taifeng_stores import SqlNamespace

    from xuanpu_server.identity.store import IdentityStore

    Connection = asyncpg.Connection | PoolConnectionProxy


class PostgresCoordinationStore:
    """复用控制面连接池；每次工作单元持有唯一数据库事务。"""
    async def runtime_contexts(self, tenant: str, user: str, session_id: str) -> tuple[str, ...]:
        """同连接只读来源索引，不在已有事务内嵌套写事务。"""
        sql = f'SELECT id FROM {self._namespace.table("shared_context")} WHERE tenant_id=$1 AND user_id=$2 AND runtime_session_id=$3 ORDER BY id'
        conn = read_connection(self._pool, sql)
        if conn is not None:
            rows = await conn.fetch(sql, tenant, user, session_id)
        else:
            async with self._pool.acquire() as current:
                rows = await current.fetch(sql, tenant, user, session_id)
        return tuple(row['id'] for row in rows)
    def __init__(self, pool: asyncpg.Pool, namespace: SqlNamespace) -> None:
        """命名空间由已验证的控制面配置提供。"""
        self._pool = pool
        self._namespace = namespace
        self._t = {n: namespace.table(n) for n in (
            'tenant', 'user', 'coordination_receipt', 'coordination_outbox', 'coordination_ack', 'audit')}

    def bind_identity(self, identity: IdentityStore) -> None:
        """身份行直接从同连接锁定，不缓存身份权限。"""

    @asynccontextmanager
    async def transaction(self, tenant: str, *, participants: tuple[str, ...] = (),
                          allow_inactive: tuple[str, ...] = (), timeout: float = 5) -> AsyncIterator[CoordinationTransaction]:
        """租户锁先于员工锁；超时包含池等待、执行和提交时间。"""
        ordered = tuple(sorted(set(participants)))
        if not set(allow_inactive) <= set(ordered):
            raise InvalidRequestError('允许停用的员工必须已加入事务')
        async with asyncio.timeout(timeout), self._pool.acquire() as conn, conn.transaction():
            await self._lock_tenant(conn, tenant)
            state = await self._load(conn, tenant)
            expanded = purpose_participants(state, ordered)
            permitted = tuple(sorted(set(allow_inactive) | (set(expanded) - set(ordered))))
            epochs, statuses = await self._lock(conn, tenant, expanded, permitted)
            before = state.model_copy(deep=True)
            tx = CoordinationTransaction(tenant, expanded, state, epochs, statuses)
            with transaction_reads(self._pool, conn):
                yield tx
            validate_commit(tenant, before, state)
            validate_shared(tx)
            validate_collaboration(tx)
            validate_profiles(tx)
            await save_organization(conn, self._namespace, tenant, before.organization, state.organization)
            await save_profiles(conn, self._namespace, tx)
            await save_shared(conn, self._namespace, tenant, before.shared, state.shared)
            await save_collaboration(conn, self._namespace, tenant, before.collaboration, state.collaboration)
            await self._save(conn, tenant, before, state)

    async def _lock_tenant(self, conn: Connection, tenant: str) -> None:
        """先取得租户锁，再读取用途关系以确定完整的排序员工锁集。"""
        row = await conn.fetchrow(f"SELECT status FROM {self._t['tenant']} WHERE tenant_id=$1 FOR UPDATE", tenant)
        if row is None:
            raise NotFoundError('租户不存在')
        if row['status'] != 'active':
            raise PermissionDeniedError('租户已停用')

    async def _lock(self, conn: Connection, tenant: str, participants: tuple[str, ...], allow_inactive: tuple[str, ...]) -> tuple[dict[str, int], dict[str, str]]:
        """全部员工按ID排序锁定，来源停用只影响相应用途，不阻断无关读取。"""
        epochs: dict[str, int] = {}
        statuses: dict[str, str] = {}
        for user in participants:
            row = await conn.fetchrow(f"SELECT status,lifecycle_epoch FROM {self._t['user']} WHERE tenant_id=$1 AND user_id=$2 FOR UPDATE", tenant, user)
            if row is None or (row['status'] != 'active' and user not in allow_inactive):
                raise PermissionDeniedError('参与员工不存在或已停用')
            epochs[user], statuses[user] = row['lifecycle_epoch'], row['status']
        return epochs, statuses

    async def _load(self, conn: Connection, tenant: str) -> CoordinationState:
        """类型化解析失败明确中止，不能吞掉损坏的记录。"""
        state = CoordinationState(collaboration=await load_collaboration(conn, self._namespace, tenant), organization=await load_organization(conn, self._namespace, tenant),
                                  shared=await load_shared(conn, self._namespace, tenant))
        for row in await conn.fetch(f"SELECT record FROM {self._t['coordination_receipt']} WHERE tenant_id=$1", tenant):
            receipt = CoordinationReceipt.model_validate_json(row['record'])
            state.receipts[receipt_key(receipt.actor, receipt.request_id)] = receipt
        for row in await conn.fetch(f"SELECT record FROM {self._t['coordination_outbox']} WHERE tenant_id=$1", tenant):
            event = CoordinationEvent.model_validate_json(row['record'])
            state.events[event.id] = event
        for row in await conn.fetch(f"SELECT consumer,event_id FROM {self._t['coordination_ack']} WHERE tenant_id=$1", tenant):
            state.acknowledgements.setdefault(row['consumer'], set()).add(row['event_id'])
        return state

    async def _save(self, conn: Connection, tenant: str, before: CoordinationState, state: CoordinationState) -> None:
        """幂等回执与outbox仅追加；治理审计与事件在同事务内提交。"""
        for key in state.receipts.keys() - before.receipts.keys():
            receipt = state.receipts[key]
            await conn.execute(f"INSERT INTO {self._t['coordination_receipt']} (tenant_id,actor,request_id,record) VALUES($1,$2,$3,$4::jsonb)",
                               tenant, receipt.actor, receipt.request_id, receipt.model_dump_json())
        for key in state.events.keys() - before.events.keys():
            event = state.events[key]
            await conn.execute(f"INSERT INTO {self._t['coordination_outbox']} (tenant_id,id,kind,entity_id,entity_version,record) VALUES($1,$2,$3,$4,$5,$6::jsonb)",
                               tenant, event.id, event.kind, event.entity_id, event.entity_version, event.model_dump_json())
            await conn.execute(f"INSERT INTO {self._t['audit']} (tenant_id,source,kind,at,actor,data) VALUES($1,'control',$2,$3,$4,$5::jsonb)",
                               tenant, event.kind if '.' in event.kind else 'organization.' + event.kind, event.at, event.actor,
                               json.dumps({'event_id': event.id, 'entity_id': event.entity_id, 'entity_version': event.entity_version}))
        for consumer, ids in state.acknowledgements.items():
            for event_id in ids - before.acknowledgements.get(consumer, set()):
                await conn.execute(f"INSERT INTO {self._t['coordination_ack']} (tenant_id,consumer,event_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                                   tenant, consumer, event_id)

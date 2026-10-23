"""连接域PG后端，复用控制面池与租户行锁。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from xuanpu_server.connections.store import ConnectionState
from xuanpu_server.errors import NotFoundError
from xuanpu_server.transaction_reads import read_connection, transaction_reads

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg
    from taifeng_stores import SqlNamespace


class PostgresConnectionStore:
    """按租户加载连接元数据；敏感密文使用独立内部表。"""
    def __init__(self,pool: asyncpg.Pool,namespace: SqlNamespace) -> None:
        """表名只来自已校验的命名空间。"""
        self._pool=pool
        self._tables={n:namespace.table(n) for n in ('tenant','connection_record','connection_credential','connection_auth_attempt','audit')}

    async def read_record(self, tenant: str, identifier: str) -> dict[str, Any] | None:
        """同一协调事务直接读取当前安装行，不二次占池或重复锁租户。"""
        sql = f"SELECT record FROM {self._tables['connection_record']} WHERE tenant_id=$1 AND id=$2"
        current = read_connection(self._pool, sql)
        if current is not None:
            raw = await current.fetchval(sql, tenant, identifier)
        else:
            async with self._pool.acquire() as conn:
                raw = await conn.fetchval(sql, tenant, identifier)
        if raw is None:
            return None
        value: dict[str, Any] = json.loads(raw)
        return value

    @asynccontextmanager
    async def transaction(self,tenant: str) -> AsyncIterator[ConnectionState]:
        """固定锁序为租户→连接；异常由数据库自动回滚。"""
        t=self._tables
        async with self._pool.acquire() as conn,conn.transaction():
            if await conn.fetchval(f"SELECT tenant_id FROM {t['tenant']} WHERE tenant_id=$1 FOR UPDATE",tenant) is None:
                raise NotFoundError('租户不存在')
            records=await conn.fetch(f"SELECT id,record FROM {t['connection_record']} WHERE tenant_id=$1",tenant)
            credentials=await conn.fetch(f"SELECT id,ciphertext FROM {t['connection_credential']} WHERE tenant_id=$1",tenant)
            state=ConnectionState({r['id']:json.loads(r['record']) for r in records},
                                  {r['id']:bytes(r['ciphertext']) for r in credentials})
            old=json.dumps(state.records,sort_keys=True)
            old_credentials=dict(state.credentials)
            with transaction_reads(self._pool,conn):
                yield state
            if old!=json.dumps(state.records,sort_keys=True):
                await conn.execute(f"DELETE FROM {t['connection_record']} WHERE tenant_id=$1",tenant)
                for identifier,record in state.records.items():
                    await conn.execute(f"INSERT INTO {t['connection_record']} (tenant_id,id,record) VALUES($1,$2,$3::jsonb)",tenant,identifier,json.dumps(record))
            for identifier,ciphertext in state.credentials.items():
                if old_credentials.get(identifier)!=ciphertext:
                    await conn.execute(f"INSERT INTO {t['connection_credential']} (tenant_id,id,ciphertext) VALUES($1,$2,$3) ON CONFLICT (tenant_id,id) DO UPDATE SET ciphertext=EXCLUDED.ciphertext",tenant,identifier,ciphertext)
            for identifier in old_credentials.keys()-state.credentials.keys():
                await conn.execute(f"DELETE FROM {t['connection_credential']} WHERE tenant_id=$1 AND id=$2",tenant,identifier)
            for event in state.audits:
                await conn.execute(f"INSERT INTO {t['audit']} (tenant_id,source,kind,at,actor,user_id,data) VALUES($1,'control',$2,$3,$4,$5,$6::jsonb)",tenant,event['kind'],event['at'],event['actor'],event.get('user_id'),json.dumps(event['data']))

    async def put_attempt(self,state_hash: str,record: dict[str,Any],*,now: float) -> None:
        """认证状态独立于业务元数据，跨实例回调仍只消费一次。"""
        from xuanpu_server.errors import ConflictError

        t=self._tables
        async with self._pool.acquire() as conn,conn.transaction():
            await conn.execute(f"SELECT tenant_id FROM {t['tenant']} WHERE tenant_id=$1 FOR UPDATE",record['tenant_id'])
            await conn.execute(f"DELETE FROM {t['connection_auth_attempt']} WHERE expires_at<=$1",now)
            count=await conn.fetchval(f"SELECT count(*) FROM {t['connection_auth_attempt']} WHERE tenant_id=$1",record['tenant_id'])
            if count>=1000:
                raise ConflictError('登录尝试过多，请稍后重试')
            await conn.execute(f"INSERT INTO {t['connection_auth_attempt']} (state_hash,tenant_id,expires_at,record) VALUES($1,$2,$3,$4::jsonb)",state_hash,record['tenant_id'],record['expires_at'],json.dumps(record))

    async def take_attempt(self,state_hash: str,*,now: float) -> dict[str,Any] | None:
        """DELETE RETURNING确保并发只消费一次，过期同样删除。"""
        async with self._pool.acquire() as conn:
            row=await conn.fetchrow(f"DELETE FROM {self._tables['connection_auth_attempt']} WHERE state_hash=$1 RETURNING record,expires_at",state_hash)
        if row is None or row['expires_at']<=now:
            return None
        result: dict[str,Any]=json.loads(row['record'])
        return result

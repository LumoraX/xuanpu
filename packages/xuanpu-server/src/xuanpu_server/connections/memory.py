"""连接域内存后端：提交时替换快照，取消和异常均回滚。"""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from xuanpu_server.connections.store import ConnectionState
from xuanpu_server.controlplane.models import AuditRecord

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


class InMemoryConnectionStore:
    """与PG相同的事务语义；不作为生产凭据持久化。"""
    def __init__(self,audit: Callable[[list[AuditRecord]],None] | None=None) -> None:
        """初始化租户隔离的锁和已提交状态。"""
        self._audit=audit
        self._history: list[AuditRecord]=[]
        self._attempts: dict[str,dict[str,Any]] = {}
        self._states: dict[str,ConnectionState] = {}
        self._locks: dict[str,asyncio.Lock] = {}

    @asynccontextmanager
    async def transaction(self,tenant: str) -> AsyncIterator[ConnectionState]:
        """所有回执、epoch和密文原子提交。"""
        async with self._locks.setdefault(tenant,asyncio.Lock()):
            state=copy.deepcopy(self._states.get(tenant,ConnectionState()))
            state.audits=[]
            yield state
            records=[AuditRecord(tenant_id=tenant,source='control',kind=e['kind'],at=e['at'],actor=e['actor'],user_id=e.get('user_id'),data=e['data']) for e in state.audits]
            if self._audit is not None:
                self._audit(records)
            self._history.extend(records)
            self._states[tenant]=copy.deepcopy(state)

    async def put_attempt(self,state_hash: str,record: dict[str,Any],*,now: float) -> None:
        """内存动作无await中断，与PG原子写入语义一致。"""
        from xuanpu_server.errors import ConflictError

        self._attempts={k:v for k,v in self._attempts.items() if v['expires_at']>now}
        if sum(v['tenant_id']==record['tenant_id'] for v in self._attempts.values())>=1000:
            raise ConflictError('登录尝试过多，请稍后重试')
        self._attempts[state_hash]=copy.deepcopy(record)

    async def take_attempt(self,state_hash: str,*,now: float) -> dict[str,Any] | None:
        """跨浏览器或超时也无法重放。"""
        record=self._attempts.pop(state_hash,None)
        return record if record is not None and record['expires_at']>now else None

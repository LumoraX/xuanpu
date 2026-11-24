"""人物与关系的内存存储实现，按员工锁与隔离快照提供与 PostgreSQL 相同的事务语义。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING

from server.core.controlplane.models import AuditRecord
from server.domains.people.models import PeopleState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable


class MemoryPeopleStore:
    """按 (tenant_id, user_id) 隔离的内存人物与关系仓储。"""

    def __init__(self, audit: Callable[[list[AuditRecord]], None] | None = None) -> None:
        """初始化空表与员工锁映射。"""
        self._states: dict[tuple[str, str], PeopleState] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._audit = audit

    def _lock(self, tenant: str, user: str) -> asyncio.Lock:
        """为每个 (tenant, user) 懒创建排他锁。"""
        key = (tenant, user)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def snapshot(self, tenant: str, user: str) -> PeopleState:
        """返回本人人物空间的隔离只读副本。"""
        current = self._states.get((tenant, user))
        return deepcopy(current) if current is not None else PeopleState()

    @asynccontextmanager
    async def transaction(
        self,
        tenant: str,
        user: str,
        guard: Callable[[], Awaitable[None]],
    ) -> AsyncIterator[PeopleState]:
        """持员工锁校验身份，异常自动回滚，提交时同步写入脱敏审计。"""
        async with self._lock(tenant, user):
            await guard()
            original = self._states.get((tenant, user))
            working = deepcopy(original) if original is not None else PeopleState()
            working.audit.clear()
            yield working
            audits = [
                AuditRecord(
                    tenant_id=tenant,
                    user_id=user,
                    source="control",
                    kind=item["kind"],
                    at=item["at"],
                    actor="user:" + user,
                    data=deepcopy(item["data"]),
                )
                for item in working.audit
            ]
            if self._audit is not None and audits:
                self._audit(audits)
            working.audit.clear()
            self._states[(tenant, user)] = working

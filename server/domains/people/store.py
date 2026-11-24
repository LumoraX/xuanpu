"""人物与关系存储契约：内存与 PostgreSQL 共享同一事务接口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from server.domains.people.models import PeopleState


class PeopleStore(Protocol):
    """按 (tenant_id, user_id) 隔离的人物空间持久化协议。"""

    def transaction(
        self,
        tenant: str,
        user: str,
        guard: Callable[[], Awaitable[None]],
    ) -> AbstractAsyncContextManager[PeopleState]:
        """持员工排他锁打开可回滚的领域事务，提交时一并落盘审计元数据。"""
        ...

    async def snapshot(self, tenant: str, user: str) -> PeopleState:
        """读取单员工此刻的只读快照，不加写锁。"""
        ...

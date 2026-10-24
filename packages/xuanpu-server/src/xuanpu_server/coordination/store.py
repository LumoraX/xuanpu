"""统一协调事务协议，组织及后续共享领域共用工作单元。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.identity.store import IdentityStore


class CoordinationStore(Protocol):
    """固定租户→排序员工→领域实体→outbox，禁止在事务内等待外部服务。"""

    async def runtime_contexts(self, tenant: str, user: str, session_id: str) -> tuple[str, ...]:
        """仅查询本人业务会话的来源上下文编号，不返回正文或授权缓存。"""
        ...
    def bind_identity(self, identity: IdentityStore) -> None:
        """内存后端取得同装配的当前身份状态，PG直接锁同库身份行。"""
        ...

    def transaction(self, tenant: str, *, participants: tuple[str, ...] = (),
                    allow_inactive: tuple[str, ...] = (), timeout: float = 5) -> AbstractAsyncContextManager[CoordinationTransaction]:
        """短事务异常、超时和取消一并回滚。"""
        ...

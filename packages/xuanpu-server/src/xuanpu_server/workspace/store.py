"""个人工作台后端的原子员工事务协议。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from xuanpu_server.workspace.models import WorkspaceState


class WorkspaceStore(Protocol):
    """领域在员工锁内检查关系、配额、版本并原子提交审计。"""

    def transaction(
        self,
        tenant: str,
        user: str,
        guard: Callable[[], Awaitable[None]],
        request_id: str | None = None,
    ) -> AbstractAsyncContextManager[WorkspaceState]:
        """同一员工串行写入；异常回滚元数据与审计。"""
        ...

    async def messages(
        self,
        tenant: str,
        user: str,
        after_id: int,
        limit: int,
        *,
        matter_id: str | None = None,
        before_id: int | None = None,
        latest: bool = False,
    ) -> list[dict[str, Any]]:
        """按本人持久游标分页读取主对话。"""
        ...

    async def find_message(self, tenant: str, user: str, request_id: str) -> dict[str, Any] | None:
        """按本人请求唯一索引读取缓存结果。"""
        ...

    async def pending_proposals(self, tenant: str, user: str) -> list[dict[str, Any]]:
        """先排除同请求终态，再读最近一百条助手建议。"""
        ...

    async def proposal_result(
        self, tenant: str, user: str, request_id: str
    ) -> dict[str, Any] | None:
        """读取创建或取消与领域同事务保存的回执。"""
        ...

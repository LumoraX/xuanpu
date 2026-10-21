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
        *, allow_inactive: bool = False,
    ) -> AbstractAsyncContextManager[WorkspaceState]:
        """同一员工串行写入；异常回滚元数据与审计。"""
        ...

    async def trigger_backlog(self) -> dict[str, int | float | None]:
        """内部汇总未结算投递数量与最老时间，不返回员工或材料内容。"""
        ...

    async def trigger_candidates(self,kind: str, *, after: tuple[str,str,str] | None=None,limit: int=100,now: float) -> list[tuple[str,str,str]]:
        """只查询有界due定义或逐条未结算投递。"""
        ...

    async def trigger_occurrences(self,tenant: str,user: str,identifier: str, *, after: str | None=None,limit: int=100) -> list[dict[str,Any]]:
        """本人发生历史分页。"""
        ...

    async def durable_candidates(self, *, after: tuple[str, str] | None = None,
                                 limit: int = 100, now: float) -> list[tuple[str, str, str]]:
        """按员工轮转读取有界due候选，不返回任务正文。"""
        ...

    async def task_owners(self, after: tuple[str, str] | None = None,
                          limit: int = 100, *, legacy_only: bool = False) -> list[tuple[str, str]]:
        """仅供进程生命周期恢复枚举任务所有者，含已停用员工。"""
        ...

    async def owners(
        self,
        after: tuple[str, str] | None = None,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        """稳定分页枚举已有工作区，后台仍须在提交前复查员工权限。"""
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

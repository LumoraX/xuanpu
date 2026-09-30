"""租户配额的检查。

配额在两个入口检查：建会话时看活动会话数，发消息时看当天的 token 用量。用量在一轮对话
结束时才入账，所以当天的实际用量可能超出上限一轮对话的量——上限拦的是「下一轮」。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from xuanpu_server.errors import NotFoundError, PermissionDeniedError, QuotaExceededError

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.tenancy import Tenant


def usage_day(at: float) -> str:
    """某个时刻所属的用量日（UTC）。"""
    return datetime.fromtimestamp(at, tz=UTC).strftime("%Y-%m-%d")


class QuotaGuard:
    """配额检查。"""

    def __init__(self, store: ControlPlaneStore, *, now: Callable[[], float] = time.time) -> None:
        """
        Args:
            store: 控制面存储。
            now: 取当前时间的函数，测试时可替换。
        """
        self._store = store
        self._now = now

    async def active_tenant(self, tenant_id: str) -> Tenant:
        """取租户并确认它可以使用平台。"""
        tenant = await self._store.get_tenant(tenant_id)
        if tenant is None:
            raise NotFoundError("租户不存在")
        if tenant.status != "active":
            raise PermissionDeniedError("租户已被停用")
        return tenant

    async def check_new_session(self, tenant_id: str) -> None:
        """建会话之前检查活动会话数。"""
        tenant = await self.active_tenant(tenant_id)
        limit = tenant.quota.max_active_sessions
        if limit is None:
            return
        active = await self._store.count_active_sessions(tenant_id)
        if active >= limit:
            raise QuotaExceededError(f"活动会话数已达上限 {limit}，请先关闭不用的会话")

    async def check_new_turn(self, tenant_id: str) -> None:
        """发消息之前检查当天的 token 用量。"""
        tenant = await self.active_tenant(tenant_id)
        limit = tenant.quota.max_daily_tokens
        if limit is None:
            return
        usage = await self._store.get_usage(tenant_id, usage_day(self._now()))
        if usage.total_tokens >= limit:
            raise QuotaExceededError(f"今日 token 用量已达上限 {limit}")


__all__ = ["QuotaGuard", "usage_day"]

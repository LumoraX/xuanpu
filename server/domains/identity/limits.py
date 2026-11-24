"""按租户限住同时在调飞书的请求数。

飞书登录的回调不需要凭据：谁拿着自己开始的一次登录尝试，都能让平台去调一次飞书。限住每个租户的
并发，一个租户被刷时占不满平台的出站连接，也刷不光这家企业飞书应用的接口额度。超出上限的排队，
排队有时限，等不到报 ``CallLimitBusyError``，不无限排队。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

FEISHU_CALLS_PER_TENANT = 8
"""每个租户同时在调飞书的登录最多这么多个。一次正常登录（换令牌加取用户信息）只占几百毫秒，
8 个足够应付一家企业早上集中登录。"""
FEISHU_QUEUE_SECONDS = 5.0
"""超出上限的排队等多久（秒）。"""


class CallLimitBusyError(Exception):
    """排队超时：这个租户同时在调飞书的请求已达上限，等了 ``queue_seconds`` 还没轮到。"""


class TenantCallLimit:
    """每个租户一个信号量。键只会是存在且启用的租户，个数以租户数为限。

    Attributes:
        limit: 每个租户的并发上限。
        queue_seconds: 排队的时限（测试时可调小）。
    """

    def __init__(self, *, limit: int, queue_seconds: float) -> None:
        """
        Args:
            limit: 每个租户的并发上限。
            queue_seconds: 排队的时限。
        """
        self.limit = limit
        self.queue_seconds = queue_seconds
        self._slots: dict[str, asyncio.Semaphore] = {}

    @asynccontextmanager
    async def slot(self, tenant_id: str) -> AsyncIterator[None]:
        """占一个名额，退出时归还。

        Raises:
            CallLimitBusyError: 排队超时。
        """
        slots = self._slots.get(tenant_id)
        if slots is None:
            slots = self._slots[tenant_id] = asyncio.Semaphore(self.limit)
        try:
            async with asyncio.timeout(self.queue_seconds):
                await slots.acquire()
        except TimeoutError as exc:
            raise CallLimitBusyError(
                f"同时在调飞书的请求已达 {self.limit} 个，排队 {self.queue_seconds} 秒仍未轮到"
            ) from exc
        try:
            yield
        finally:
            slots.release()


__all__ = [
    "FEISHU_CALLS_PER_TENANT",
    "FEISHU_QUEUE_SECONDS",
    "CallLimitBusyError",
    "TenantCallLimit",
]

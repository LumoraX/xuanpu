"""身份域的周期任务：清理过期的登录尝试与登录会话。

由 ``Platform.start()`` 经 ``IdentityService.start()`` 启动，``Platform.close()`` 时取消并等它结束。
每隔 ``PURGE_INTERVAL_SECONDS`` 调一次 ``IdentityStore.purge_expired_logins(now)``：没走完的登录
（开始登录不需要凭据，谁都能写一条登录尝试）与过期没退出的登录会话都在这里清掉，库里不会越积越多。

一轮出错只记日志，循环不退出，下一轮再试（存储一时连不上不该让清理永久停掉）。按飞书通讯录的
对账挂进同一个周期；对账自身用租约判断是否到期。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from xuanpu_server.identity.store import IdentityStore

logger = logging.getLogger(__name__)

PURGE_INTERVAL_SECONDS = 300.0
"""多久清理一次（秒）：5 分钟。登录尝试 10 分钟就过期，清得太勤只是白白查库；隔得太久，被刷的
登录尝试会在库里堆上一阵。过期的记录本来就不认，清理只为了不占地方，晚几分钟无妨。"""


class IdentityMaintenance:
    """身份域的周期任务。``start`` 之后每隔一段时间跑一轮 ``run_once``。"""

    def __init__(
        self,
        store: IdentityStore,
        *,
        interval_seconds: float = PURGE_INTERVAL_SECONDS,
        reconcile: Callable[[], Awaitable[None]] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 身份存储。
            interval_seconds: 两轮之间隔多久；测试时可调小。
            reconcile: 同周期检查应执行的对账，省略时只清理登录记录。
            now: 取当前时间的函数，测试时可替换。
        """
        self._reconcile = reconcile
        self._store = store
        self._interval = interval_seconds
        self._now = now
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """周期任务是否在跑。"""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """启动周期任务；已经在跑就不再起一个。第一轮在一个间隔之后，不在启动时就查库。"""
        if not self.running:
            self._task = asyncio.create_task(self._loop(), name="identity-maintenance")

    async def close(self) -> None:
        """取消周期任务并等它结束。没启动过、重复调用都可以。"""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def run_once(self) -> int:
        """跑一轮：清掉全部租户里过期的登录尝试与登录会话，返回清掉的条数。"""
        purged = await self._store.purge_expired_logins(self._now())
        if self._reconcile is not None:
            await self._reconcile()
        return purged

    async def _loop(self) -> None:
        """每隔一个间隔跑一轮；一轮出错记日志、接着跑（取消照常向外传）。"""
        while True:
            await asyncio.sleep(self._interval)
            try:
                purged = await self.run_once()
            except Exception:
                logger.exception("清理过期的登录尝试与登录会话失败，下一轮再试")
                continue
            if purged:
                logger.info("清理了 %d 条过期的登录尝试与登录会话", purged)


__all__ = ["PURGE_INTERVAL_SECONDS", "IdentityMaintenance"]

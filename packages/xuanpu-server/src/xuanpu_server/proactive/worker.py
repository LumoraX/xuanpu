"""独立规则与语义循环，慢模型不会阻塞定时检查。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xuanpu_server.proactive.service import ProactiveService

logger = logging.getLogger(__name__)


class ProactiveWorker:
    """所有后台任务由生命周期拥有，关闭时取消并回收模型任务。"""

    def __init__(self, service: ProactiveService, *, scan_seconds: float = 60.0) -> None:
        """频率由部署配置注入，不从环境变量读取。"""
        self.service, self.scan_seconds = service, scan_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """重复启动无副作用。"""
        if self._task is None:
            self._task = asyncio.create_task(self._supervise(), name='proactive-business')

    async def _supervise(self) -> None:
        """规则与语义独立执行，外部慢请求不延迟规则提醒。"""
        async with asyncio.TaskGroup() as group:
            group.create_task(self._loop('rules'), name='proactive-rules')
            group.create_task(self._loop('semantic'), name='proactive-semantic')

    async def _loop(self, kind: str) -> None:
        """有限扫描失败后重试，取消请求不能被普通错误处理吞掉。"""
        while True:
            await asyncio.sleep(self.scan_seconds)
            try:
                await self.service.run_owners(kind)
            except Exception:
                logger.exception('proactive_%s_cycle_failed', kind)

    async def close(self) -> None:
        """先结束所有在途检查，再由平台关闭模型与数据库。"""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

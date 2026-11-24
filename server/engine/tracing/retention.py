"""执行追踪的定期清理：按租户分批删除超过保留期的 trace（span 随之删除），不动审计。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.engine.tracing.config import ExecutionTraceConfig
    from server.engine.tracing.store import SpanStore

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 3600.0
BATCH = 500
MAX_BATCHES = 100


class TraceRetention:
    """每小时一轮；一轮里每个开启的租户最多删 ``BATCH × MAX_BATCHES`` 条，剩下的留到下一轮。"""

    def __init__(
        self, store: SpanStore, config: ExecutionTraceConfig, *,
        now: Callable[[], float], interval: float = INTERVAL_SECONDS,
    ) -> None:
        """配置与时钟由平台装配注入。"""
        self._store, self._config, self._now, self._interval = store, config, now, interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """追踪没开启时不起后台任务。"""
        if self._config.enabled and self._config.tenants:
            self._task = asyncio.ensure_future(self._loop())

    async def close(self) -> None:
        """停后台任务。"""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def run_once(self) -> int:
        """清理一轮，返回删除条数；单个租户失败只记日志，不影响其余租户。"""
        keep = timedelta(days=self._config.retention_days)
        cutoff = datetime.fromtimestamp(self._now(), UTC) - keep
        removed = 0
        for tenant in self._config.tenants:
            try:
                for _ in range(MAX_BATCHES):
                    count = await self._store.purge(tenant, before=cutoff, batch=BATCH)
                    removed += count
                    if count < BATCH:
                        break
            except Exception:
                logger.exception("清理租户 %s 的执行追踪失败，下一轮重试", tenant)
        return removed

    async def _loop(self) -> None:
        """启动后先等一个周期，再定时清理。"""
        while True:
            await asyncio.sleep(self._interval)
            await self.run_once()

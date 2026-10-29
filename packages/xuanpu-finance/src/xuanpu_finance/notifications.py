"""不依赖模型及外部消息渠道的原生通知后台入口。"""

from __future__ import annotations

from typing import Protocol


class NotificationStore(Protocol):
    """后台只需要派生本地站内索引的最小能力。"""

    async def refresh_finance_notifications(self, *, batch_size: int = 100) -> int:
        """刷新有界当前来源与旧Outbox。"""
        ...


class NotificationsWorker:
    """不会执行付款、税务、归档或外部消息发送的后台任务。"""

    def __init__(self, store: NotificationStore, *, batch_size: int = 100) -> None:
        """显式注入独立财务存储和每次扫描预算。"""
        self.store, self.batch_size = store, batch_size

    async def run_once(self) -> int:
        """仅刷新本地索引，由运行时负责取消、禁用和实例锁。"""
        return await self.store.refresh_finance_notifications(batch_size=self.batch_size)

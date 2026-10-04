"""有界提醒扫描和可恢复外部投递工作者。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from xuanpu_server.reminders.models import DeliveryError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.reminders.models import (
        DeliveryRecord,
        NotificationRecord,
        NotificationSender,
    )
    from xuanpu_server.reminders.store import ReminderStore

logger = logging.getLogger(__name__)


class ReminderWorker:
    """进程可重启，状态全在持久存储中；不在数据库事务内等待网络。"""

    def __init__(
        self,
        *,
        store: ReminderStore,
        control: ControlPlaneStore,
        identity: IdentityStore,
        sender: NotificationSender | None,
        now: Callable[[], float],
    ) -> None:
        """依赖与时钟由服务装配。"""
        self._store = store
        self._control = control
        self._identity = identity
        self._sender = sender
        self._now = now
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """启动受监督的站内扫描和外部投递两个独立循环。"""
        if self._task is None:
            self._task = asyncio.create_task(self._supervise(), name="reminder-worker")

    async def close(self) -> None:
        """取消扫描、网络或休眠，并回收任务。"""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _supervise(self) -> None:
        """同一任务组拥有两个循环，关闭时同时取消并等待它们。"""
        async with asyncio.TaskGroup() as group:
            group.create_task(self._loop(self.scan_once, "scan"), name="reminder-scan")
            group.create_task(self._loop(self.deliver_once, "delivery"), name="reminder-delivery")

    async def _loop(self, action: Callable[[], Awaitable[None]], name: str) -> None:
        """每个循环独立恢复异常，慢外部网络不会阻塞下一轮到期扫描。"""
        while True:
            try:
                await action()
            except Exception:
                logger.error("reminder_%s_cycle_failed", name)
            await asyncio.sleep(1)

    async def run_once(self) -> None:
        """确定性单轮入口，测试可顺序处理到期提醒与投递。"""
        await self.scan_once()
        await self.deliver_once()

    async def scan_once(self) -> None:
        """只处理最多一百个到期候选，不等待外部网络。"""
        for record in await self._store.due(self._now()):
            try:
                await self._store.fire(record, self._now())
            except Exception:
                logger.error("reminder_fire_failed")

    async def deliver_once(self) -> None:
        """处理一批有界投递，任务组保证失败和关闭时无遗留发送。"""
        deliveries = await self._store.claim_deliveries(self._now())
        # 并发发送让本批任务均能在30秒租期内完成一次10秒的网络调用。
        async with asyncio.TaskGroup() as group:
            for delivery, notification in deliveries:
                group.create_task(self._send(delivery, notification))

    async def _send(self, record: DeliveryRecord, notification: NotificationRecord) -> None:
        """每次发送重新检查身份，超时及临时错误在五次以内退避。"""
        try:
            tenant = await self._control.get_tenant(record.tenant_id)
            user = await self._identity.get_user(record.tenant_id, record.user_id)
            if user is None or user.status != "active":
                await self._store.finish_delivery(
                    record, "skipped", self._now(), "identity_disabled"
                )
                return
            if tenant is None or tenant.status != "active":
                await self._store.finish_delivery(record, "pending", self._now(), "tenant_paused")
                return
            # 预留整个发送超时，避免过期租约与接手工作者同时发出请求。
            if self._now() + 10 > record.lease_until:
                return
            if self._sender is None:
                raise DeliveryError("channel_unconfigured", retryable=False)
            await asyncio.wait_for(self._sender.send(notification, record.channel), timeout=10)
        except DeliveryError as exc:
            status = (
                "pending"
                if exc.code == "tenant_paused" or (exc.retryable and record.attempts < 5)
                else "failed"
            )
            await self._store.finish_delivery(record, status, self._now(), exc.code)
        except TimeoutError:
            status = "pending" if record.attempts < 5 else "failed"
            await self._store.finish_delivery(record, status, self._now(), "delivery_timeout")
        except Exception:
            logger.error("reminder_delivery_failed")
            status = "pending" if record.attempts < 5 else "failed"
            await self._store.finish_delivery(record, status, self._now(), "delivery_error")
        else:
            await self._store.finish_delivery(record, "sent", self._now())

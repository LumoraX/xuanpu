"""员工原生提醒服务：身份、校验、审计与后台生命周期。"""

from __future__ import annotations

import secrets
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, InvalidRequestError, PermissionDeniedError
from xuanpu_server.reminders.models import ReminderInput, ReminderRecord
from xuanpu_server.reminders.worker import ReminderWorker

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.reminders.models import (
        DeliveryRecord,
        NotificationRecord,
        NotificationSender,
    )
    from xuanpu_server.reminders.store import ReminderStore
    from xuanpu_server.tenancy import Principal


class ReminderService:
    """以员工和租户为边界的提醒用例。"""

    def __init__(
        self,
        *,
        store: ReminderStore,
        control: ControlPlaneStore,
        identity: IdentityStore,
        audit: AuditLog,
        sender: NotificationSender | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """注入同后端存储、身份、发送器与时钟。"""
        self.store = store
        self._control = control
        self._identity = identity
        self._audit = audit
        self._sender = sender
        self._now = now
        self._worker = ReminderWorker(
            store=store, control=control, identity=identity, sender=sender, now=now
        )

    async def require_employee(self, who: Principal) -> tuple[str, str]:
        """所有操作都需要真实且当前启用的员工身份。"""
        if who.tenant_id is None or who.user_id is None:
            raise PermissionDeniedError("提醒功能需要员工身份")
        tenant = await self._control.get_tenant(who.tenant_id)
        user = await self._identity.get_user(who.tenant_id, who.user_id)
        if tenant is None or tenant.status != "active" or user is None or user.status != "active":
            raise PermissionDeniedError("员工或租户不可用")
        return who.tenant_id, who.user_id

    async def channels(self, who: Principal) -> builtins.list[str]:
        """站内信始终可用，外部渠道取适配器能力。"""
        await self.require_employee(who)
        external = await self._sender.available(who) if self._sender is not None else []
        return ["inbox", *[channel for channel in external if channel == "feishu"]]

    async def _validate_channels(self, who: Principal, body: ReminderInput) -> None:
        """不允许把未配置的渠道保存成看似可发送。"""
        if "feishu" in body.channels and "feishu" not in await self.channels(who):
            raise InvalidRequestError("该员工尚未配置飞书通知")

    async def create(self, who: Principal, body: ReminderInput, request_id: str) -> ReminderRecord:
        """创建提醒；原创建指纹用于幂等，时间额度判断交由事务。"""
        tenant, user = await self.require_employee(who)
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise InvalidRequestError("request_id 必须是1至128字的非空字符串")
        existing = await self.store.get_request(tenant, user, request_id)
        if existing is not None:
            if existing.original_fingerprint != body.fingerprint():
                raise ConflictError("request_id 已用于其他提醒内容")
            return existing
        await self._validate_channels(who, body)
        now = self._now()
        record = ReminderRecord(
            "rem_" + secrets.token_hex(12),
            tenant,
            user,
            body.title,
            body.due_at,
            body.timezone,
            body.repeat,
            tuple(body.channels),
            "active",
            1,
            request_id,
            now,
            now,
            body.fingerprint(),
        )
        saved = await self.store.create(record, now, actor=who.actor)
        return saved

    async def list(
        self, who: Principal, *, status: str | None = None
    ) -> builtins.list[ReminderRecord]:
        """列本人的最近提醒。"""
        tenant, user = await self.require_employee(who)
        if status not in (None, "active", "completed", "cancelled"):
            raise InvalidRequestError("提醒状态过滤无效")
        return await self.store.list(tenant, user, status=status)

    async def update(
        self, who: Principal, reminder_id: str, body: ReminderInput, version: int
    ) -> ReminderRecord:
        """编辑活动提醒并保留创建身份和原始指纹。"""
        tenant, user = await self.require_employee(who)
        await self._validate_channels(who, body)
        old = await self.store.get(tenant, user, reminder_id)
        record = replace(
            old,
            title=body.title,
            due_at=body.due_at,
            timezone=body.timezone,
            repeat=body.repeat,
            channels=tuple(body.channels),
        )
        saved = await self.store.update(record, version, self._now(), actor=who.actor)
        return saved

    async def cancel(self, who: Principal, reminder_id: str, version: int) -> ReminderRecord:
        """取消本人活动提醒。"""
        tenant, user = await self.require_employee(who)
        saved = await self.store.cancel(
            tenant, user, reminder_id, version, self._now(), actor=who.actor
        )
        return saved

    async def notifications(
        self, who: Principal, after_id: int = 0, limit: int = 100, *, latest: bool = False
    ) -> builtins.list[NotificationRecord]:
        """按游标增量读取或按最近窗口读取本人通知。"""
        tenant, user = await self.require_employee(who)
        if not 0 <= after_id <= 9223372036854775807 or not 1 <= limit <= 100:
            raise InvalidRequestError("通知查询范围无效")
        return await self.store.notifications(tenant, user, -1 if latest else after_id, limit)

    async def read(self, who: Principal, notification_id: int) -> NotificationRecord:
        """标记本人通知已读。"""
        tenant, user = await self.require_employee(who)
        return await self.store.read(tenant, user, notification_id, self._now())

    async def deliveries(
        self, who: Principal, notification_id: int
    ) -> builtins.list[DeliveryRecord]:
        """只查该员工的外部投递状态。"""
        tenant, user = await self.require_employee(who)
        return await self.store.deliveries(tenant, user, notification_id)

    def start(self) -> None:
        """启动每秒扫描，重复调用幂等。"""
        self._worker.start()

    async def close(self) -> None:
        """可取消地停止后台工作。"""
        await self._worker.close()

    async def run_once(self) -> None:
        """处理一轮到期提醒和外部投递，用于确定性验证。"""
        await self._worker.run_once()

"""提醒的内存后端，检查与变更之间不让出事件循环。"""

from __future__ import annotations

import secrets
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, NotFoundError, PermissionDeniedError
from xuanpu_server.proactive.rules import alert_time
from xuanpu_server.reminders.models import DeliveryRecord, NotificationRecord, ReminderRecord
from xuanpu_server.reminders.proposals import proposal_decision
from xuanpu_server.reminders.schedule import latest_due, next_due, validate_due
from xuanpu_server.workspace.notifications import invalidated_sources, validate_notifications
from xuanpu_server.workspace.reminders import queued_reminders, reminder_audit
from xuanpu_server.workspace.rules import audit, remember, replay

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable

    from xuanpu_server.controlplane.models import AuditRecord
    from xuanpu_server.workspace.memory import InMemoryWorkspaceStore
    from xuanpu_server.workspace.models import WorkspaceState


class InMemoryReminderStore:
    """同一控制面的内存提醒、通知和投递任务。"""

    def __init__(
        self,
        tenant_active: Callable[[str], bool],
        *,
        audit: Callable[[AuditRecord], None],
        workspace: InMemoryWorkspaceStore,
    ) -> None:
        """身份在身份存储装配时绑定；没有身份一律拒绝。"""
        self._audit = audit
        self._workspace = workspace
        self._tenant_active = tenant_active
        self._user_active: Callable[[str, str], bool] = lambda tenant, user: False
        self._reminders: dict[str, ReminderRecord] = {}
        self._notifications: dict[int, NotificationRecord] = {}
        self._deliveries: dict[tuple[int, str], DeliveryRecord] = {}
        self._sequence = 0
        workspace.bind_notifications(self._prepare_notifications)

    def _prepare_notifications(
        self,
        tenant: str,
        user: str,
        state: WorkspaceState,
        old: WorkspaceState,
    ) -> Callable[[], None]:
        """无副作用准备统一通知；审计成功后在同一执行片段内提交。"""
        self._require_active(tenant, user)
        commit_reminders = self._prepare_reminders(tenant, user, state)
        delivery_updates = self._invalidated_deliveries(tenant, user, state, old)
        seen = {
            n.notification_key
            for n in self._notifications.values()
            if (n.tenant_id, n.user_id) == (tenant, user)
        }
        seen_checks = {
            n.proactive_check_id
            for n in self._notifications.values()
            if (n.tenant_id, n.user_id) == (tenant, user) and n.proactive_check_id is not None
        }
        notifications: list[NotificationRecord] = []
        deliveries: list[DeliveryRecord] = []
        for pending in validate_notifications(state):
            if pending.notification_key in seen or (
                pending.proactive_check_id is not None and pending.proactive_check_id in seen_checks
            ):
                continue
            seen.add(pending.notification_key)
            if pending.proactive_check_id is not None:
                seen_checks.add(pending.proactive_check_id)
            record = pending.record(tenant, user, self._sequence + len(notifications) + 1)
            notifications.append(record)
            deliveries.extend(
                DeliveryRecord(
                    record.notification_id,
                    tenant,
                    user,
                    channel,
                    next_attempt_at=pending.alert_after,
                )
                for channel in pending.channels
                if channel != "inbox"
            )

        def commit() -> None:
            """不让出执行权，游标与投递一次性可见。"""
            commit_reminders()
            self._deliveries.update(delivery_updates)
            for notification in notifications:
                self._notifications[notification.notification_id] = notification
                self._sequence = notification.notification_id
            for delivery in deliveries:
                self._deliveries[(delivery.notification_id, delivery.channel)] = delivery

        return commit

    def _invalidated_deliveries(
        self,
        tenant: str,
        user: str,
        state: WorkspaceState,
        old: WorkspaceState,
    ) -> dict[tuple[int, str], DeliveryRecord]:
        """准备来源处理和原生提醒取消引起的撤销，审计成功前不改变已保存投递。"""
        return self._delivery_invalidations(
            tenant,
            user,
            set(invalidated_sources(old, state)),
            set(state.pending_reminder_cancellations),
        )

    def _delivery_invalidations(
        self,
        tenant: str,
        user: str,
        sources: set[tuple[str, str]],
        cancellations: set[str],
    ) -> dict[tuple[int, str], DeliveryRecord]:
        """统一准备原生提醒或主动来源的旧投递撤销，保留已经发送的历史。"""
        invalid_ids = {
            record.notification_id
            for record in self._notifications.values()
            if (record.tenant_id, record.user_id) == (tenant, user)
            and (
                (record.source, record.proactive_check_id or record.proactive_followup_id or record.work_task_id)
                in sources
                or record.reminder_id is not None
                and record.reminder_id in cancellations
            )
        }
        return {
            key: replace(
                delivery,
                status="skipped",
                claim_token=None,
                lease_until=0,
                error_code="source_resolved",
            )
            for key, delivery in self._deliveries.items()
            if delivery.notification_id in invalid_ids and delivery.status in ("pending", "sending")
        }

    def _prepare_reminders(
        self,
        tenant: str,
        user: str,
        state: WorkspaceState,
    ) -> Callable[[], None]:
        """影子副本先验证额度和幂等；批量审计成功后同步提交。"""
        records = dict(self._reminders)
        for identifier in dict.fromkeys(state.pending_reminder_cancellations):
            old = records.get(identifier)
            if old is None or (old.tenant_id, old.user_id) != (tenant, user):
                raise NotFoundError("待取消提醒不存在或不属于当前员工")
            if old.status != "cancelled":
                now = time.time()
                records[identifier] = replace(
                    old, status="cancelled", version=old.version + 1, updated_at=now
                )
                reminder_audit(state, records[identifier], "cancelled", now)
        for record in queued_reminders(state, tenant, user):
            old = next(
                (
                    item
                    for item in records.values()
                    if (item.tenant_id, item.user_id, item.request_id)
                    == (tenant, user, record.request_id)
                ),
                None,
            )
            if old is not None:
                if (
                    old.original_fingerprint != record.original_fingerprint
                    or old.reminder_id != record.reminder_id
                ):
                    raise ConflictError("提醒请求已有不同内容或标识，请重新准备")
                continue
            if record.status != "active" or record.version != 1:
                raise ConflictError("事务提醒只能创建新的活动记录")
            validate_due(record.due_at, record.created_at)
            if (
                sum(
                    item.status == "active" and (item.tenant_id, item.user_id) == (tenant, user)
                    for item in records.values()
                )
                >= 100
            ):
                raise ConflictError("每名员工最多100条活动提醒")
            if record.reminder_id in records:
                raise ConflictError("提醒标识已经存在")
            records[record.reminder_id] = record
            reminder_audit(state, record, "created", record.created_at)

        def commit() -> None:
            """提交前后不让出执行权，其他员工提醒不会被过期副本覆盖。"""
            self._reminders = records

        return commit

    def bind_identity(self, user_active: Callable[[str, str], bool]) -> None:
        """绑定不让出执行权的身份查询。"""
        self._user_active = user_active

    def _active(self, tenant_id: str, user_id: str) -> bool:
        """当前租户和员工同时启用。"""
        return self._tenant_active(tenant_id) and self._user_active(tenant_id, user_id)

    def _require_active(self, tenant_id: str, user_id: str) -> None:
        """写操作必须有有效员工和租户。"""
        if not self._active(tenant_id, user_id):
            raise PermissionDeniedError("员工或租户不可用")

    def disable_user(self, tenant_id: str, user_id: str, now: float) -> None:
        """身份状态变更同一无让出区间取消提醒并跳过未完成投递。"""
        self._workspace.disable_user(tenant_id, user_id, now)
        for key, item in self._reminders.items():
            if (item.tenant_id, item.user_id, item.status) == (tenant_id, user_id, "active"):
                self._reminders[key] = replace(
                    item, status="cancelled", version=item.version + 1, updated_at=now
                )
        for delivery_key, delivery in self._deliveries.items():
            if (delivery.tenant_id, delivery.user_id) == (
                tenant_id,
                user_id,
            ) and delivery.status in ("pending", "sending"):
                self._deliveries[delivery_key] = replace(
                    delivery,
                    status="skipped",
                    claim_token=None,
                    lease_until=0,
                    error_code="identity_disabled",
                )

    async def create(
        self,
        record: ReminderRecord,
        now: float,
        *,
        actor: str | None = None,
        proposal_id: str | None = None,
    ) -> ReminderRecord:
        """普通创建无让出；建议确认与工作台决定共享员工事务。"""
        if proposal_id is not None:
            return await self._create_proposal(record, now, actor, proposal_id)
        prepared = self._prepare_create(record, now)
        if prepared.reminder_id not in self._reminders:
            self._audit_event(prepared, "created", now, actor)
            self._reminders[prepared.reminder_id] = prepared
        return prepared

    def _prepare_create(self, record: ReminderRecord, now: float) -> ReminderRecord:
        """只做创建校验，任何失败都发生在审计与域写入前。"""
        self._require_active(record.tenant_id, record.user_id)
        owned = [
            r
            for r in self._reminders.values()
            if (r.tenant_id, r.user_id) == (record.tenant_id, record.user_id)
        ]
        existing = next((r for r in owned if r.request_id == record.request_id), None)
        if existing is not None:
            if existing.original_fingerprint != record.original_fingerprint:
                raise ConflictError("request_id 已用于其他提醒内容")
            return existing
        validate_due(record.due_at, now)
        if sum(r.status == "active" for r in owned) >= 100:
            raise ConflictError("每名员工最多100条活动提醒")
        return record

    async def _create_proposal(
        self, record: ReminderRecord, now: float, actor: str | None, proposal_id: str
    ) -> ReminderRecord:
        """决定、提醒审计及提醒创建同生共死，失败不冻结用户草稿。"""
        key, payload = proposal_decision(record, proposal_id)

        async def guard() -> None:
            """检查身份不让出执行权，保持内存提交不可分割。"""
            self._require_active(record.tenant_id, record.user_id)

        async with self._workspace.transaction(
            record.tenant_id, record.user_id, guard, key
        ) as state:
            prior = replay(state, key, payload)
            prepared = self._prepare_create(record, now)
            if prior is None:
                audit(state, actor or "user:" + record.user_id, "proposal_reserved", now, {})
            remember(
                state,
                key,
                payload,
                {"state": "confirmed", "result": {"reminder_id": prepared.reminder_id}},
            )
            if prepared.reminder_id not in self._reminders:
                state.audits.append(
                    {
                        "actor": actor or "user:" + record.user_id,
                        "kind": "reminder.created",
                        "at": now,
                        "data": {"reminder_id": prepared.reminder_id, "version": prepared.version},
                    }
                )
        # 退出事务的身份检查和批量审计都没有让出点，紧接着提交提醒。
        self._reminders[prepared.reminder_id] = prepared
        return prepared

    async def get_request(
        self, tenant_id: str, user_id: str, request_id: str
    ) -> ReminderRecord | None:
        """按本人原始创建请求查幂等记录。"""
        return next(
            (
                r
                for r in self._reminders.values()
                if (r.tenant_id, r.user_id, r.request_id) == (tenant_id, user_id, request_id)
            ),
            None,
        )

    async def get(self, tenant_id: str, user_id: str, reminder_id: str) -> ReminderRecord:
        """取本人记录。"""
        record = self._reminders.get(reminder_id)
        if record is None or (record.tenant_id, record.user_id) != (tenant_id, user_id):
            raise NotFoundError("提醒不存在")
        return record

    async def list(
        self, tenant_id: str, user_id: str, *, status: str | None = None
    ) -> builtins.list[ReminderRecord]:
        """按创建时间倒序取本人最近一百条。"""
        records = [
            r
            for r in self._reminders.values()
            if (r.tenant_id, r.user_id) == (tenant_id, user_id)
            and (status is None or r.status == status)
        ]
        return sorted(records, key=lambda r: (r.created_at, r.reminder_id), reverse=True)[:100]

    async def update(
        self,
        record: ReminderRecord,
        version: int,
        now: float,
        *,
        actor: str | None = None,
    ) -> ReminderRecord:
        """同一员工锁内编辑提醒、关联等待计划和旧投递资格。"""
        async with self._workspace.employee_lock(record.tenant_id, record.user_id):
            old = await self.get(record.tenant_id, record.user_id, record.reminder_id)
            self._require_active(record.tenant_id, record.user_id)
            if old.status != "active" or old.version != version:
                raise ConflictError("提醒状态或版本已变化")
            validate_due(record.due_at, now)
            updated = replace(
                record,
                version=old.version + 1,
                original_fingerprint=old.original_fingerprint,
                updated_at=now,
            )
            plan = (
                self._workspace.prepare_reminder_change(updated, now, cancelled=False)
                if old.due_at != updated.due_at
                else None
            )
            identifiers = plan[0] if plan is not None else []
            deliveries = self._delivery_invalidations(
                record.tenant_id,
                record.user_id,
                {("proactive_followup", key) for key in identifiers},
                {record.reminder_id} if identifiers else set(),
            )
            self._audit_event(updated, "updated", now, actor)
            if plan is not None:
                plan[1]()
            self._deliveries.update(deliveries)
            self._reminders[record.reminder_id] = updated
            return updated

    async def cancel(
        self,
        tenant_id: str,
        user_id: str,
        reminder_id: str,
        version: int,
        now: float,
        *,
        actor: str | None = None,
    ) -> ReminderRecord:
        """取消与关联跟进一起提交，重复取消不重复推进关注版本。"""
        async with self._workspace.employee_lock(tenant_id, user_id):
            record = await self.get(tenant_id, user_id, reminder_id)
            self._require_active(tenant_id, user_id)
            if record.status == "cancelled":
                return record
            if record.status != "active" or record.version != version:
                raise ConflictError("提醒状态或版本已变化")
            updated = replace(record, status="cancelled", version=version + 1, updated_at=now)
            identifiers, commit = self._workspace.prepare_reminder_change(
                updated, now, cancelled=True
            )
            deliveries = self._delivery_invalidations(
                tenant_id,
                user_id,
                {("proactive_followup", key) for key in identifiers},
                {reminder_id},
            )
            self._audit_event(updated, "cancelled", now, actor)
            commit()
            self._deliveries.update(deliveries)
            self._reminders[reminder_id] = updated
            return updated

    def _audit_event(
        self,
        record: ReminderRecord,
        action: str,
        now: float,
        actor: str | None = None,
        notification_id: int | None = None,
    ) -> None:
        """只记领域标识与版本；正文从不进入审计。"""
        from xuanpu_server.controlplane.models import AuditRecord

        data: dict[str, object] = {"reminder_id": record.reminder_id, "version": record.version}
        if notification_id is not None:
            data["notification_id"] = notification_id
        self._audit(
            AuditRecord(
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                source="control",
                kind="reminder." + action,
                at=now,
                actor=actor or "user:" + record.user_id,
                data=data,
            )
        )

    async def due(self, now: float) -> builtins.list[ReminderRecord]:
        """暂停租户不占用全局一百条扫描窗口。"""
        return sorted(
            (
                r
                for r in self._reminders.values()
                if r.status == "active" and r.due_at <= now and self._active(r.tenant_id, r.user_id)
            ),
            key=lambda r: (r.due_at, r.reminder_id),
        )[:100]

    async def fire(self, record: ReminderRecord, now: float) -> NotificationRecord | None:
        """提醒与主动检查在同一员工锁内分配全局通知序号。"""
        async with self._workspace.employee_lock(record.tenant_id, record.user_id):
            return await self._fire(record, now)

    async def _fire(self, record: ReminderRecord, now: float) -> NotificationRecord | None:
        """先完成计算和同步审计，再无让出提交通知、投递及提醒状态。"""
        current = await self.get(record.tenant_id, record.user_id, record.reminder_id)
        if not self._user_active(record.tenant_id, record.user_id):
            self.disable_user(record.tenant_id, record.user_id, now)
            return None
        if not self._tenant_active(record.tenant_id):
            return None
        if current.status != "active" or current.version != record.version or current.due_at > now:
            return None
        due = latest_due(current.due_at, current.timezone, current.repeat, now)
        settings = self._workspace.proactive_reminder_settings(
            record.tenant_id, record.user_id, record.reminder_id
        )
        alert_after = alert_time(settings, now) if settings is not None else now
        notification = NotificationRecord(
            self._sequence + 1,
            record.tenant_id,
            record.user_id,
            record.reminder_id,
            current.title,
            due,
            now,
            alert_after=alert_after,
        )
        following = next_due(current.due_at, current.timezone, current.repeat, now)
        updated = replace(
            current,
            due_at=following if following is not None else current.due_at,
            status="completed" if following is None else "active",
            version=current.version + 1,
            updated_at=now,
        )
        deliveries = [
            DeliveryRecord(
                notification.notification_id,
                record.tenant_id,
                record.user_id,
                channel,
                next_attempt_at=alert_after,
            )
            for channel in current.channels
            if channel != "inbox"
        ]
        self._audit_event(updated, "fired", now, "system:reminders", notification.notification_id)
        self._sequence = notification.notification_id
        self._notifications[notification.notification_id] = notification
        for delivery in deliveries:
            self._deliveries[(delivery.notification_id, delivery.channel)] = delivery
        self._reminders[record.reminder_id] = updated
        return notification

    async def notifications(
        self, tenant_id: str, user_id: str, after_id: int, limit: int
    ) -> builtins.list[NotificationRecord]:
        """读取本人持久游标后的通知。"""
        found = [
            n
            for n in self._notifications.values()
            if (n.tenant_id, n.user_id) == (tenant_id, user_id) and n.notification_id > after_id
        ]
        return found[-limit:] if after_id == -1 else found[:limit]

    async def notification_current(self, record: NotificationRecord) -> bool:
        """当前业务来源只在通知所属员工快照中解析。"""
        reminder = self._reminders.get(record.reminder_id or "")
        status = (
            reminder.status
            if reminder is not None
            and (reminder.tenant_id, reminder.user_id) == (record.tenant_id, record.user_id)
            else None
        )
        return self._workspace.notification_current(record, status)

    async def read(
        self, tenant_id: str, user_id: str, notification_id: int, now: float
    ) -> NotificationRecord:
        """已读时间仅首次写入。"""
        item = self._notifications.get(notification_id)
        if item is None or (item.tenant_id, item.user_id) != (tenant_id, user_id):
            raise NotFoundError("通知不存在")
        updated = replace(item, read_at=item.read_at if item.read_at is not None else now)
        self._notifications[notification_id] = updated
        return updated

    async def claim_deliveries(
        self, now: float
    ) -> builtins.list[tuple[DeliveryRecord, NotificationRecord]]:
        """重查员工后认领到期任务，租期30秒。"""
        found = []
        for key, record in self._deliveries.items():
            ready = record.status == "pending" and record.next_attempt_at <= now
            expired = record.status == "sending" and record.lease_until <= now
            if not (ready or expired):
                continue
            if not self._user_active(record.tenant_id, record.user_id):
                self.disable_user(record.tenant_id, record.user_id, now)
                continue
            if not self._tenant_active(record.tenant_id):
                continue
            if not await self.notification_current(self._notifications[record.notification_id]):
                self._deliveries[key] = replace(
                    record,
                    status="skipped",
                    claim_token=None,
                    lease_until=0,
                    error_code="source_resolved",
                )
                continue
            if record.first_attempt_at is not None and now >= record.first_attempt_at + 3600:
                self._deliveries[key] = replace(
                    record,
                    status="failed",
                    claim_token=None,
                    lease_until=0,
                    error_code="delivery_unknown",
                )
                continue
            if record.attempts >= 5:
                self._deliveries[key] = replace(
                    record, status="failed", claim_token=None, error_code="attempts_exhausted"
                )
                continue
            claimed = replace(
                record,
                status="sending",
                attempts=record.attempts + 1,
                claim_token=secrets.token_hex(16),
                lease_until=now + 30,
                first_attempt_at=record.first_attempt_at
                if record.first_attempt_at is not None
                else now,
            )
            self._deliveries[key] = claimed
            found.append((claimed, self._notifications[record.notification_id]))
            if len(found) == 100:
                break
        return found

    async def finish_delivery(
        self, record: DeliveryRecord, status: str, now: float, error_code: str | None = None
    ) -> None:
        """只有本次有效租约能回写。"""
        if not self._user_active(record.tenant_id, record.user_id):
            self.disable_user(record.tenant_id, record.user_id, now)
            return
        key = (record.notification_id, record.channel)
        current = self._deliveries[key]
        if (
            current.status != "sending"
            or current.claim_token != record.claim_token
            or current.lease_until <= now
        ):
            return
        next_at = now + (1, 5, 30, 120)[min(current.attempts - 1, 3)]
        self._deliveries[key] = replace(
            current,
            status=status,
            next_attempt_at=next_at,
            lease_until=0,
            claim_token=None,
            error_code=error_code,
            attempts=current.attempts - 1 if error_code == "tenant_paused" else current.attempts,
            first_attempt_at=None
            if error_code == "tenant_paused" and current.attempts == 1
            else current.first_attempt_at,
        )

    async def deliveries(
        self, tenant_id: str, user_id: str, notification_id: int
    ) -> builtins.list[DeliveryRecord]:
        """只读本人通知的外部投递状态。"""
        return [
            d
            for d in self._deliveries.values()
            if (d.tenant_id, d.user_id, d.notification_id) == (tenant_id, user_id, notification_id)
        ]

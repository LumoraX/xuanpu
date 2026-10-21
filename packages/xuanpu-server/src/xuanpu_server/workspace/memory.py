"""用于本机的个人工作台内存事务后端。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError
from xuanpu_server.reminders.notification_state import (
    native_notification_current,
    proactive_notification_current,
)
from xuanpu_server.workspace.models import WorkspaceState
from xuanpu_server.workspace.notifications import validate_notifications, validate_proactive
from xuanpu_server.workspace.reminders import synchronize_reminder_followups
from xuanpu_server.workspace.rules import close_proactive, ensure_todo_progress

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from xuanpu_server.controlplane.models import AuditRecord
    from xuanpu_server.reminders.models import NotificationRecord, ReminderRecord


class InMemoryWorkspaceStore:
    """按员工加锁，私有副本验证后一次提交。"""

    def __init__(self, audit: Callable[[list[AuditRecord]], None]) -> None:
        """注入同步批量审计，在提交前记录。"""
        self._audit = audit
        self._states: dict[tuple[str, str], WorkspaceState] = {}
        self._epochs: dict[tuple[str, str], int] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._notification_sink: (
            Callable[[str, str, WorkspaceState, WorkspaceState], Callable[[], None]] | None
        ) = None

    def disable_user(self, tenant: str, user: str, now: float) -> None:
        """身份停用同步关闭旧关注，并使已打开的事务快照失效。"""
        owner = tenant, user
        self._epochs[owner] = self._epochs.get(owner, 0) + 1
        state = self._states.get(owner)
        if state is not None:
            for item_id in state.items:
                close_proactive(state, item_id, now)
            for record in state.followups.values():
                if record.get("status") not in ("resolved", "dismissed"):
                    record.update(
                        status="resolved",
                        handled_at=now,
                        updated_at=now,
                        lease_until=0,
                        claim_token=None,
                    )
            # 身份停用由提醒存储在同一执行片段内取消全部提醒，无须留下事务队列。
            state.pending_reminder_cancellations = []

    def proactive_reminder_settings(
        self,
        tenant: str,
        user: str,
        reminder_id: str,
    ) -> dict[str, Any] | None:
        """员工锁内同步读取本人跟进关联与偏好，不让出原生提醒提交区间。"""
        state = self._states.get((tenant, user))
        if state is None or not any(
            record.get("reminder_id") == reminder_id for record in state.followups.values()
        ):
            return None
        return deepcopy(state.settings)

    def notification_current(
        self, notification: NotificationRecord, reminder_status: str | None
    ) -> bool:
        """同步读取本人业务来源，通知认领与工作区提交之间不让出执行权。"""
        state = self._states.get((notification.tenant_id, notification.user_id), WorkspaceState())
        if notification.source == "reminder":
            sources = [
                (record, state.items.get(record.get("item_id", "")))
                for record in state.followups.values()
                if record.get("reminder_id") == notification.reminder_id
            ]
            return native_notification_current(notification, reminder_status, sources)
        if notification.source not in ("proactive_check", "proactive_followup"):
            return False
        records = state.checks if notification.source == "proactive_check" else state.followups
        identifier = (
            notification.proactive_check_id
            if notification.source == "proactive_check"
            else notification.proactive_followup_id
        )
        record = records.get(identifier or "")
        item = state.items.get(record.get("item_id", "")) if record else None
        return proactive_notification_current(notification, record, item)

    def prepare_reminder_change(
        self,
        reminder: ReminderRecord,
        now: float,
        *,
        cancelled: bool,
    ) -> tuple[list[str], Callable[[], None]]:
        """员工锁内准备跟进同步，提醒审计成功后才无让出提交工作区副本。"""
        owner = reminder.tenant_id, reminder.user_id
        state = deepcopy(self._states.get(owner, WorkspaceState()))
        identifiers = synchronize_reminder_followups(
            state.followups, reminder, now, cancelled=cancelled
        )

        def commit() -> None:
            """全部校验和提醒审计成功之后才替换关联跟进计划。"""
            if identifiers:
                self._states[owner] = state

        return identifiers, commit

    def employee_lock(self, tenant: str, user: str) -> asyncio.Lock:
        """提醒和工作区共用员工锁，分配通知游标后直到提交不释放。"""
        return self._locks.setdefault((tenant, user), asyncio.Lock())

    def bind_notifications(
        self,
        sink: Callable[[str, str, WorkspaceState, WorkspaceState], Callable[[], None]],
    ) -> None:
        """绑定先准备后同步提交的统一通知存储。"""
        self._notification_sink = sink

    async def task_owners(self, after: tuple[str, str] | None = None,
                          limit: int = 100) -> list[tuple[str, str]]:
        """内部恢复枚举任务所有者，包含已停用员工以完成中断结算。"""
        from xuanpu_server.work_tasks.models import needs_recovery

        if not 1 <= limit <= 100:
            raise ValueError('分页数量须为1至100')
        return sorted(owner for owner, state in self._states.items()
            if (after is None or owner > after) and any(needs_recovery(value)
                for value in state.work_tasks.values()))[:limit]

    async def owners(
        self,
        after: tuple[str, str] | None = None,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        """内存仅枚举已提交员工，身份由后台提交前 guard 复查。"""
        if not 1 <= limit <= 100:
            raise ValueError("分页数量须为1至100")
        return sorted(owner for owner in self._states if after is None or owner > after)[:limit]

    @asynccontextmanager
    async def transaction(
        self,
        tenant: str,
        user: str,
        guard: Callable[[], Awaitable[None]],
        request_id: str | None = None,
        *, allow_inactive: bool = False,
    ) -> AsyncIterator[WorkspaceState]:
        """复查身份且失败不替换原状态。"""
        from xuanpu_server.controlplane.models import AuditRecord

        owner = (tenant, user)
        async with self.employee_lock(tenant, user):
            if not allow_inactive:
                await guard()
            epoch = self._epochs.get(owner, 0)
            original = self._states.get(owner, WorkspaceState())
            state = deepcopy(original)
            state.audits = []
            for item in state.items.values():
                ensure_todo_progress(item)
            yield state
            if not allow_inactive:
                await guard()
            if epoch != self._epochs.get(owner, 0):
                raise ConflictError("员工身份已变化，请重新读取工作区")
            validate_proactive(state)
            validate_notifications(state)
            commit_notifications = None
            if (
                state.pending_notifications
                or state.pending_reminders
                or state.pending_reminder_cancellations
                or state.checks != original.checks
                or state.followups != original.followups
            ):
                if self._notification_sink is None:
                    raise RuntimeError("统一通知存储未绑定")
                commit_notifications = self._notification_sink(tenant, user, state, original)
            audits = [
                AuditRecord(tenant_id=tenant, user_id=user, source="control", **a)
                for a in state.audits
            ]
            self._audit(audits)
            if commit_notifications is not None:
                commit_notifications()
            state.pending_notifications = []
            state.pending_reminders = []
            state.pending_reminder_actors = {}
            state.pending_reminder_cancellations = []
            state.audits = []
            self._states[owner] = deepcopy(state)

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
        """返回独立副本，调用者无法修改已保存消息。"""
        state = self._states.get((tenant, user), WorkspaceState())
        found = [
            m
            for m in state.messages
            if m["id"] > after_id
            and (before_id is None or m["id"] < before_id)
            and (matter_id is None or m["matter_id"] == matter_id)
        ]
        return deepcopy(found[-limit:] if latest else found[:limit])

    async def find_message(self, tenant: str, user: str, request_id: str) -> dict[str, Any] | None:
        """利用幂等字典按请求查已保存消息。"""
        state = self._states.get((tenant, user), WorkspaceState())
        request = state.requests.get(request_id)
        if request is None or "role" not in request["result"]:
            return None
        return dict(deepcopy(request["result"]))

    async def pending_proposals(self, tenant: str, user: str) -> list[dict[str, Any]]:
        """用请求索引排除已确认或取消，聊天不会占分页额度。"""
        state = self._states.get((tenant, user), WorkspaceState())
        found: list[dict[str, Any]] = []
        message_requests = {message["request_id"] for message in state.messages}
        for message in reversed(state.messages):
            request_id = message["request_id"]
            if message["role"] != "assistant" or not request_id.endswith(":assistant"):
                continue
            route = message.get("metadata", {}).get("route", {})
            if not isinstance(route, dict) or not any(
                isinstance(route.get(k), dict) for k in ("workspace_draft", "reminder_draft")
            ):
                continue
            base = request_id[:-10]
            decision = state.requests.get("proposal:" + base)
            if decision is not None and decision["result"].get("state") in (
                "confirmed",
                "cancelled",
            ):
                continue
            if base + ":confirmed" in message_requests or base + ":cancelled" in message_requests:
                continue
            found.append(message)
            if len(found) == 100:
                break
        return deepcopy(found)

    async def proposal_result(
        self, tenant: str, user: str, request_id: str
    ) -> dict[str, Any] | None:
        """没有终态聊天也可通过请求索引恢复已提交回执。"""
        state = self._states.get((tenant, user), WorkspaceState())
        decision = state.requests.get("proposal:" + request_id)
        if decision is None or decision["result"].get("state") not in ("confirmed", "cancelled"):
            return None
        return dict(deepcopy(decision["result"]))

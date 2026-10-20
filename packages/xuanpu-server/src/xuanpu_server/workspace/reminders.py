"""工作区协调事务中原生提醒队列的公共校验和审计。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.reminders.models import ReminderInput, ReminderRecord

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState


def queued_reminders(state: WorkspaceState, tenant: str, user: str) -> list[ReminderRecord]:
    """服务端准备的提醒仍须严格校验，禁止向其他员工队列写入。"""
    found = []
    for value in state.pending_reminders:
        try:
            payload = dict(value)
            body = ReminderInput.model_validate(
                {
                    key: list(payload[key]) if key == "channels" else payload[key]
                    for key in ("title", "due_at", "timezone", "repeat", "channels")
                }
            )
            payload["channels"] = tuple(body.channels)
            record = ReminderRecord(**payload)
        except (TypeError, KeyError, ValidationError) as exc:
            raise InvalidRequestError("事务提醒字段无效") from exc
        if (record.tenant_id, record.user_id) != (tenant, user):
            raise InvalidRequestError("事务提醒不属于当前员工")
        if not record.request_id or not record.reminder_id or len(record.request_id) > 128:
            raise InvalidRequestError("事务提醒请求标识无效")
        found.append(record)
    return found


def reminder_audit(state: WorkspaceState, record: ReminderRecord, action: str, now: float) -> None:
    """协调创建和取消沿用原生提醒审计，正文不入审计。"""
    state.audits.append(
        {
            "actor": "system:proactive",
            "kind": "reminder." + action,
            "at": now,
            "data": {"reminder_id": record.reminder_id, "version": record.version},
        }
    )


def synchronize_reminder_followups(
    records: dict[str, dict[str, Any]],
    reminder: ReminderRecord,
    now: float,
    *,
    cancelled: bool,
) -> list[str]:
    """提醒管理入口共用跟进时间与终态规则，只修改调用者已限定归属的活动关联。"""
    changed = []
    for identifier, record in records.items():
        if record.get("reminder_id") != reminder.reminder_id or record.get("status") in (
            "resolved",
            "dismissed",
            "ignored",
        ):
            continue
        record.update(
            status="dismissed" if cancelled else "waiting",
            version=record.get("version", 1) + 1,
            updated_at=now,
            handled_at=now,
            lease_until=0,
            claim_token=None,
        )
        if not cancelled:
            record.update(due_at=reminder.due_at, next_review_at=reminder.due_at)
        changed.append(identifier)
    return changed

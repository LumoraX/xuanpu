"""员工工作区事务内原生提醒创建、取消与幂等检查。"""

from __future__ import annotations

import time
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.reminders.postgres import RC, reminder
from xuanpu_server.reminders.schedule import validate_due
from xuanpu_server.workspace.reminders import queued_reminders, reminder_audit

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState


async def save_reminders(
    conn: Any,
    tables: dict[str, str],
    tenant: str,
    user: str,
    state: WorkspaceState,
) -> None:
    """复用已持有的身份锁，不开启第二连接或第二事务。"""
    table = tables["reminder"]
    for identifier in dict.fromkeys(state.pending_reminder_cancellations):
        row = await conn.fetchrow(
            f"SELECT {RC} FROM {table} WHERE tenant_id=$1 AND user_id=$2 AND reminder_id=$3 FOR UPDATE",
            tenant,
            user,
            identifier,
        )
        if row is None:
            raise NotFoundError("待取消提醒不存在或不属于当前员工")
        record = reminder(row)
        await conn.execute(
            f"UPDATE {tables['notification_delivery']} d SET status='skipped',claim_token=NULL,lease_until=0,error_code='source_resolved' "
            f"FROM {tables['notification']} n WHERE d.notification_id=n.notification_id "
            "AND d.tenant_id=$1 AND d.user_id=$2 AND n.tenant_id=$1 AND n.user_id=$2 "
            "AND n.reminder_id=$3 AND d.status IN ('pending','sending')",
            tenant,
            user,
            identifier,
        )
        if record.status != "cancelled":
            now = time.time()
            await conn.execute(
                f"UPDATE {table} SET status='cancelled',version=version+1,updated_at=$4 "
                "WHERE tenant_id=$1 AND user_id=$2 AND reminder_id=$3",
                tenant,
                user,
                identifier,
                now,
            )
            reminder_audit(state, replace(record, version=record.version + 1), "cancelled", now)
    for record in queued_reminders(state, tenant, user):
        row = await conn.fetchrow(
            f"SELECT {RC} FROM {table} WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3",
            tenant,
            user,
            record.request_id,
        )
        if row is not None:
            old = reminder(row)
            if (
                old.original_fingerprint != record.original_fingerprint
                or old.reminder_id != record.reminder_id
            ):
                raise ConflictError("提醒请求已有不同内容或标识，请重新准备")
            continue
        if record.status != "active" or record.version != 1:
            raise ConflictError("事务提醒只能创建新的活动记录")
        validate_due(record.due_at, record.created_at)
        count = await conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE tenant_id=$1 AND user_id=$2 AND status='active'",
            tenant,
            user,
        )
        if count >= 100:
            raise ConflictError("每名员工最多100条活动提醒")
        await conn.execute(
            f"INSERT INTO {table} ({RC}) VALUES ({','.join(f'${n}' for n in range(1, 15))})",
            *asdict(record).values(),
        )
        reminder_audit(state, record, "created", record.created_at)

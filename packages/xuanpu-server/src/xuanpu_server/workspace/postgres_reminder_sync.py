"""原生提醒管理入口与主动跟进等待计划的同事务同步。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.workspace.reminders import synchronize_reminder_followups

if TYPE_CHECKING:
    from xuanpu_server.reminders.models import ReminderRecord


async def sync_reminder_followups(
    conn: Any,
    tables: dict[str, str],
    reminder: ReminderRecord,
    now: float,
    *,
    cancelled: bool,
) -> None:
    """在已持有员工锁的事务内改跟进并撤销旧通知，不跨员工或租户查询。"""
    tenant, user = reminder.tenant_id, reminder.user_id
    rows = await conn.fetch(
        f"SELECT followup_id,record FROM {tables['proactive_followup']} "
        "WHERE tenant_id=$1 AND user_id=$2 AND record->>'reminder_id'=$3 FOR UPDATE",
        tenant,
        user,
        reminder.reminder_id,
    )
    records = {row["followup_id"]: json.loads(row["record"]) for row in rows}
    identifiers = synchronize_reminder_followups(records, reminder, now, cancelled=cancelled)
    for identifier in identifiers:
        await conn.execute(
            f"UPDATE {tables['proactive_followup']} SET record=$4::jsonb "
            "WHERE tenant_id=$1 AND user_id=$2 AND followup_id=$3",
            tenant,
            user,
            identifier,
            json.dumps(records[identifier]),
        )
    if identifiers or cancelled:
        await conn.execute(
            f"UPDATE {tables['notification_delivery']} d SET status='skipped',claim_token=NULL,lease_until=0,error_code='source_resolved' "
            f"FROM {tables['notification']} n WHERE d.notification_id=n.notification_id "
            "AND d.tenant_id=$1 AND d.user_id=$2 AND n.tenant_id=$1 AND n.user_id=$2 "
            "AND (n.reminder_id=$3 OR n.proactive_followup_id=ANY($4::text[])) AND d.status IN ('pending','sending')",
            tenant,
            user,
            reminder.reminder_id,
            identifiers,
        )

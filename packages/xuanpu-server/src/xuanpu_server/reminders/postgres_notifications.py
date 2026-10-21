"""按租户员工读取统一通知的当前来源，不在只读核实过程中改变历史。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.reminders.notification_state import (
    native_notification_current,
    proactive_notification_current,
)

if TYPE_CHECKING:
    from xuanpu_server.reminders.models import NotificationRecord


async def notification_current(
    conn: Any, tables: dict[str, str], notification: NotificationRecord
) -> bool:
    """调用者可传入已持有员工行锁的连接，避免认领事务内再开第二连接。"""
    tenant, user = notification.tenant_id, notification.user_id
    if notification.source == "reminder":
        rows = await conn.fetch(
            f"SELECT r.status,f.record,i.record AS item FROM {tables['reminder']} r "
            f"LEFT JOIN {tables['proactive_followup']} f ON f.tenant_id=r.tenant_id AND f.user_id=r.user_id "
            "AND f.record->>'reminder_id'=r.reminder_id "
            f"LEFT JOIN {tables['workspace_item']} i ON i.tenant_id=f.tenant_id AND i.user_id=f.user_id "
            "AND i.item_id=f.record->>'item_id' WHERE r.tenant_id=$1 AND r.user_id=$2 AND r.reminder_id=$3",
            tenant,
            user,
            notification.reminder_id,
        )
        status = rows[0]["status"] if rows else None
        sources = [
            (
                json.loads(row["record"]),
                json.loads(row["item"]) if row["item"] is not None else None,
            )
            for row in rows
            if row["record"] is not None
        ]
        return native_notification_current(notification, status, sources)
    if notification.source=='task_definition':
        return bool(await conn.fetchval(f"SELECT NOT enabled FROM {tables['task_definition']} WHERE tenant_id=$1 AND user_id=$2 AND definition_id=$3",
                                       tenant,user,notification.task_definition_id))
    if notification.source not in ("proactive_check", "proactive_followup"):
        return False
    table, key, identifier = (
        ("proactive_check", "check_id", notification.proactive_check_id)
        if notification.source == "proactive_check"
        else ("proactive_followup", "followup_id", notification.proactive_followup_id)
    )
    row = await conn.fetchrow(
        f"SELECT p.record,i.record AS item FROM {tables[table]} p "
        f"LEFT JOIN {tables['workspace_item']} i ON i.tenant_id=p.tenant_id AND i.user_id=p.user_id "
        f"AND i.item_id=p.record->>'item_id' WHERE p.tenant_id=$1 AND p.user_id=$2 AND p.{key}=$3",
        tenant,
        user,
        identifier,
    )
    if row is None:
        return False
    return proactive_notification_current(
        notification,
        json.loads(row["record"]),
        json.loads(row["item"]) if row["item"] is not None else None,
    )

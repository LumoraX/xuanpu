"""工作区事务内的主动状态持久化与统一通知 outbox。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.workspace.notifications import (
    invalidated_sources,
    validate_notifications,
    validate_proactive,
)

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState


async def load_proactive(
    conn: Any, tables: dict[str, str], tenant: str, user: str, state: WorkspaceState
) -> None:
    """员工已加排他锁，读取完整关注状态和任意结构扫描游标。"""
    for table, key, target in (
        ("proactive_check", "check_id", state.checks),
        ("proactive_followup", "followup_id", state.followups),
    ):
        rows = await conn.fetch(
            f"SELECT {key},record FROM {tables[table]} WHERE tenant_id=$1 AND user_id=$2",
            tenant,
            user,
        )
        target.update({row[key]: json.loads(row["record"]) for row in rows})
    record = await conn.fetchval(
        f"SELECT record FROM {tables['proactive_scan_state']} WHERE tenant_id=$1 AND user_id=$2",
        tenant,
        user,
    )
    if record is not None:
        state.scan_state = json.loads(record)


async def save_proactive(
    conn: Any,
    tables: dict[str, str],
    tenant: str,
    user: str,
    old: WorkspaceState,
    state: WorkspaceState,
) -> None:
    """先保存来源，再分配通知序号，沿用员工锁直到事务最终提交。"""
    validate_proactive(state)
    pending = validate_notifications(state)
    for source, identifier in invalidated_sources(old, state):
        column = "proactive_check_id" if source == "proactive_check" else "proactive_followup_id"
        await conn.execute(
            f"UPDATE {tables['notification_delivery']} d SET status='skipped',claim_token=NULL,lease_until=0,error_code='source_resolved' "
            f"FROM {tables['notification']} n WHERE d.notification_id=n.notification_id "
            "AND d.tenant_id=$1 AND d.user_id=$2 AND n.tenant_id=$1 AND n.user_id=$2 "
            f"AND n.{column}=$3 AND d.status IN ('pending','sending')",
            tenant,
            user,
            identifier,
        )
    for table, key, before, after in (
        ("proactive_check", "check_id", old.checks, state.checks),
        ("proactive_followup", "followup_id", old.followups, state.followups),
    ):
        for identifier, record in after.items():
            if before.get(identifier) != record:
                await conn.execute(
                    f"INSERT INTO {tables[table]} (tenant_id,user_id,{key},record) VALUES ($1,$2,$3,$4::jsonb) "
                    f"ON CONFLICT (tenant_id,user_id,{key}) DO UPDATE SET record=EXCLUDED.record",
                    tenant,
                    user,
                    identifier,
                    json.dumps(record),
                )
    if old.scan_state != state.scan_state:
        await conn.execute(
            f"INSERT INTO {tables['proactive_scan_state']} (tenant_id,user_id,record) VALUES ($1,$2,$3::jsonb) "
            "ON CONFLICT (tenant_id,user_id) DO UPDATE SET record=EXCLUDED.record",
            tenant,
            user,
            json.dumps(state.scan_state),
        )
    for notification in pending:
        notification_id = await conn.fetchval(
            f"INSERT INTO {tables['notification']} "
            "(tenant_id,user_id,notification_key,source,proactive_check_id,proactive_followup_id,title,body,due_at,created_at,alert_after,task_definition_id) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) "
            "ON CONFLICT DO NOTHING RETURNING notification_id",
            tenant,
            user,
            notification.notification_key,
            notification.source,
            notification.proactive_check_id,
            notification.proactive_followup_id,
            notification.title,
            notification.body,
            notification.due_at,
            notification.created_at,
            notification.alert_after,
            notification.task_definition_id,
        )
        if notification_id is not None:
            for channel in notification.channels:
                if channel != "inbox":
                    await conn.execute(
                        f"INSERT INTO {tables['notification_delivery']} (notification_id,tenant_id,user_id,channel,next_attempt_at) "
                        "VALUES ($1,$2,$3,$4,$5)",
                        notification_id,
                        tenant,
                        user,
                        channel,
                        notification.alert_after,
                    )

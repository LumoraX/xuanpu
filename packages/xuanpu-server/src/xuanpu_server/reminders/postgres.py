"""提醒 PostgreSQL 后端；所有事务按租户、员工、提醒的顺序加锁。"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.models import AuditRecord
from xuanpu_server.controlplane.schema import DEFAULT_NAMESPACE
from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.proactive.rules import alert_time
from xuanpu_server.reminders.models import NotificationRecord, ReminderRecord
from xuanpu_server.reminders.postgres_delivery import PostgresDeliveryStore
from xuanpu_server.reminders.proposals import proposal_decision
from xuanpu_server.reminders.schedule import latest_due, next_due, validate_due
from xuanpu_server.workspace.models import fingerprint
from xuanpu_server.workspace.postgres_reminder_sync import sync_reminder_followups

if TYPE_CHECKING:
    import builtins

    import asyncpg
    from taifeng_stores import SqlNamespace

RC = "reminder_id,tenant_id,user_id,title,due_at,timezone,repeat,channels,status,version,request_id,created_at,updated_at,original_fingerprint"
NC = "notification_id,tenant_id,user_id,reminder_id,title,due_at,created_at,read_at,notification_key,source,body,proactive_check_id,proactive_followup_id,alert_after,task_definition_id,work_task_id"


def reminder(row: asyncpg.Record) -> ReminderRecord:
    """数据库行转冻结记录。"""
    fields = dict(row)
    fields["channels"] = tuple(fields["channels"])
    return ReminderRecord(**fields)


class PostgresReminderStore(PostgresDeliveryStore):
    """在同一连接池、命名空间内持久化提醒。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """由控制面装配同后端存储。"""
        super().__init__(pool, namespace=namespace)
        self._t["workspace_request"] = namespace.table("workspace_request")
        self._t["workspace_settings"] = namespace.table("workspace_settings")
        self._t["proactive_followup"] = namespace.table("proactive_followup")

    async def create(
        self,
        record: ReminderRecord,
        now: float,
        *,
        actor: str | None = None,
        proposal_id: str | None = None,
    ) -> ReminderRecord:
        """幂等先于时间和额度，在员工锁内分配活动额度。"""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._require_active(conn, record.tenant_id, record.user_id)
            if proposal_id is not None:
                await self._reserve_proposal(conn, record, proposal_id, now, actor)
            old = await conn.fetchrow(
                f"SELECT {RC} FROM {self._t['reminder']} WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3",
                record.tenant_id,
                record.user_id,
                record.request_id,
            )
            if old is not None:
                existing = reminder(old)
                if existing.original_fingerprint != record.original_fingerprint:
                    raise ConflictError("request_id 已用于其他提醒内容")
                if proposal_id is not None:
                    await self._complete_proposal(conn, existing, proposal_id)
                return existing
            validate_due(record.due_at, now)
            count = await conn.fetchval(
                f"SELECT count(*) FROM {self._t['reminder']} WHERE tenant_id=$1 AND user_id=$2 AND status='active'",
                record.tenant_id,
                record.user_id,
            )
            if count >= 100:
                raise ConflictError("每名员工最多100条活动提醒")
            await conn.execute(
                f"INSERT INTO {self._t['reminder']} ({RC}) VALUES ({','.join(f'${n}' for n in range(1, 15))})",
                *asdict(record).values(),
            )
            await self._record_audit(conn, record, "created", now, actor)
            if proposal_id is not None:
                await self._complete_proposal(conn, record, proposal_id)
            return record

    async def _reserve_proposal(
        self, conn: Any, record: ReminderRecord, proposal_id: str, now: float, actor: str | None
    ) -> None:
        """员工行锁内决定与提醒共用当前事务，错误自然回滚预留。"""
        key, payload = proposal_decision(record, proposal_id)
        digest = fingerprint(payload)
        old = await conn.fetchrow(
            f"SELECT fingerprint FROM {self._t['workspace_request']} WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3",
            record.tenant_id,
            record.user_id,
            key,
        )
        if old is not None:
            if old["fingerprint"] != digest:
                raise ConflictError("建议动作或确认内容已变化")
            return
        await conn.execute(
            f"INSERT INTO {self._t['workspace_request']} (tenant_id,user_id,request_id,fingerprint,result) VALUES ($1,$2,$3,$4,$5::jsonb)",
            record.tenant_id,
            record.user_id,
            key,
            digest,
            json.dumps({}),
        )
        await self._audit_event(
            conn,
            AuditRecord(
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                source="control",
                kind="workspace.proposal_reserved",
                at=now,
                actor=actor or "user:" + record.user_id,
                data={},
            ),
        )

    async def _complete_proposal(self, conn: Any, record: ReminderRecord, proposal_id: str) -> None:
        """提醒标识在同一提交中成为权威回执，终态消息只作展示。"""
        await conn.execute(
            f"UPDATE {self._t['workspace_request']} SET result=$4::jsonb WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3",
            record.tenant_id,
            record.user_id,
            "proposal:" + proposal_id,
            json.dumps({"state": "confirmed", "result": {"reminder_id": record.reminder_id}}),
        )

    async def get_request(
        self, tenant_id: str, user_id: str, request_id: str
    ) -> ReminderRecord | None:
        """按本人原始创建请求查幂等记录。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {RC} FROM {self._t['reminder']} WHERE tenant_id=$1 AND user_id=$2 AND request_id=$3",
                tenant_id,
                user_id,
                request_id,
            )
        return reminder(row) if row is not None else None

    async def get(self, tenant_id: str, user_id: str, reminder_id: str) -> ReminderRecord:
        """只查本人的提醒。"""
        async with self._pool.acquire() as conn:
            return await self._get(conn, tenant_id, user_id, reminder_id)

    async def _get(
        self, conn: Any, tenant_id: str, user_id: str, reminder_id: str, *, lock: bool = False
    ) -> ReminderRecord:
        """取指定提醒，可在员工锁之后再加提醒锁。"""
        row = await conn.fetchrow(
            f"SELECT {RC} FROM {self._t['reminder']} WHERE tenant_id=$1 AND user_id=$2 AND reminder_id=$3"
            + (" FOR UPDATE" if lock else ""),
            tenant_id,
            user_id,
            reminder_id,
        )
        if row is None:
            raise NotFoundError("提醒不存在")
        return reminder(row)

    async def list(
        self, tenant_id: str, user_id: str, *, status: str | None = None
    ) -> builtins.list[ReminderRecord]:
        """本人最近一百条。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {RC} FROM {self._t['reminder']} WHERE tenant_id=$1 AND user_id=$2 AND ($3::text IS NULL OR status=$3) ORDER BY created_at DESC, reminder_id DESC LIMIT 100",
                tenant_id,
                user_id,
                status,
            )
        return [reminder(row) for row in rows]

    async def update(
        self, record: ReminderRecord, version: int, now: float, *, actor: str | None = None
    ) -> ReminderRecord:
        """锁内复查版本，独立保留原始创建指纹。"""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._require_active(conn, record.tenant_id, record.user_id)
            old = await self._get(
                conn, record.tenant_id, record.user_id, record.reminder_id, lock=True
            )
            if old.status != "active" or old.version != version:
                raise ConflictError("提醒状态或版本已变化")
            validate_due(record.due_at, now)
            row = await conn.fetchrow(
                f"UPDATE {self._t['reminder']} SET title=$4,due_at=$5,timezone=$6,repeat=$7,channels=$8,version=version+1,updated_at=$9 WHERE tenant_id=$1 AND user_id=$2 AND reminder_id=$3 RETURNING {RC}",
                record.tenant_id,
                record.user_id,
                record.reminder_id,
                record.title,
                record.due_at,
                record.timezone,
                record.repeat,
                list(record.channels),
                now,
            )
            assert row is not None
            updated = reminder(row)
            if old.due_at != updated.due_at:
                await sync_reminder_followups(conn, self._t, updated, now, cancelled=False)
            await self._record_audit(conn, updated, "updated", now, actor)
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
        """取消活动提醒或幂等返回已取消的提醒。"""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._require_active(conn, tenant_id, user_id)
            old = await self._get(conn, tenant_id, user_id, reminder_id, lock=True)
            if old.status == "cancelled":
                return old
            if old.status != "active" or old.version != version:
                raise ConflictError("提醒状态或版本已变化")
            await conn.execute(
                f"UPDATE {self._t['reminder']} SET status='cancelled',version=version+1,updated_at=$4 WHERE tenant_id=$1 AND user_id=$2 AND reminder_id=$3",
                tenant_id,
                user_id,
                reminder_id,
                now,
            )
            updated = replace(old, status="cancelled", version=version + 1, updated_at=now)
            await sync_reminder_followups(conn, self._t, updated, now, cancelled=True)
            await self._record_audit(conn, updated, "cancelled", now, actor)
            return updated

    async def _record_audit(
        self,
        conn: Any,
        record: ReminderRecord,
        action: str,
        now: float,
        actor: str | None = None,
        notification_id: int | None = None,
    ) -> None:
        """同事务审计只保留标识和版本，不包含提醒正文。"""
        data: dict[str, object] = {"reminder_id": record.reminder_id, "version": record.version}
        if notification_id is not None:
            data["notification_id"] = notification_id
        await self._audit_event(
            conn,
            AuditRecord(
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                source="control",
                kind="reminder." + action,
                at=now,
                actor=actor or "user:" + record.user_id,
                data=data,
            ),
        )

    async def due(self, now: float) -> builtins.list[ReminderRecord]:
        """扫描全局最多一百候选，所有写事务重新带回租户员工。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {','.join('r.' + column for column in RC.split(','))} FROM {self._t['reminder']} r JOIN {self._t['tenant']} t ON t.tenant_id=r.tenant_id AND t.status='active' JOIN {self._t['user']} u ON u.tenant_id=r.tenant_id AND u.user_id=r.user_id AND u.status='active' WHERE r.status='active' AND r.due_at<=$1 ORDER BY r.due_at,r.reminder_id LIMIT 100",
                now,
            )
        return [reminder(row) for row in rows]

    async def _reminder_alert_after(self, conn: Any, record: ReminderRecord, now: float) -> float:
        """持有员工锁时仅对本人主动跟进关联的提醒应用当前免打扰设置。"""
        settings = await conn.fetchval(
            f"SELECT s.settings FROM {self._t['workspace_settings']} s "
            "WHERE s.tenant_id=$1 AND s.user_id=$2 AND EXISTS ("
            f"SELECT 1 FROM {self._t['proactive_followup']} f "
            "WHERE f.tenant_id=s.tenant_id AND f.user_id=s.user_id AND f.record->>'reminder_id'=$3)",
            record.tenant_id,
            record.user_id,
            record.reminder_id,
        )
        return alert_time(json.loads(settings), now) if settings is not None else now

    async def fire(self, record: ReminderRecord, now: float) -> NotificationRecord | None:
        """事务内一次提交站内通知、外部投递和下一到期时间。"""
        async with self._pool.acquire() as conn, conn.transaction():
            tenant_active, user_active = await self._identity_state(
                conn, record.tenant_id, record.user_id
            )
            if not user_active:
                await self._disable(conn, record.tenant_id, record.user_id, now)
                return None
            if not tenant_active:
                return None
            current = await self._get(
                conn, record.tenant_id, record.user_id, record.reminder_id, lock=True
            )
            if (
                current.status != "active"
                or current.version != record.version
                or current.due_at > now
            ):
                return None
            alert_after = await self._reminder_alert_after(conn, current, now)
            row = await conn.fetchrow(
                f"INSERT INTO {self._t['notification']} (tenant_id,user_id,reminder_id,title,due_at,created_at,alert_after) VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (reminder_id,due_at) DO NOTHING RETURNING {NC}",
                current.tenant_id,
                current.user_id,
                current.reminder_id,
                current.title,
                latest_due(current.due_at, current.timezone, current.repeat, now),
                now,
                alert_after,
            )
            if row is None:
                return None
            notification = NotificationRecord(**dict(row))
            for channel in current.channels:
                if channel != "inbox":
                    await conn.execute(
                        f"INSERT INTO {self._t['notification_delivery']} (notification_id,tenant_id,user_id,channel,next_attempt_at) VALUES ($1,$2,$3,$4,$5)",
                        notification.notification_id,
                        current.tenant_id,
                        current.user_id,
                        channel,
                        alert_after,
                    )
            following = next_due(current.due_at, current.timezone, current.repeat, now)
            await conn.execute(
                f"UPDATE {self._t['reminder']} SET due_at=$4,status=$5,version=version+1,updated_at=$6 WHERE tenant_id=$1 AND user_id=$2 AND reminder_id=$3",
                current.tenant_id,
                current.user_id,
                current.reminder_id,
                following if following is not None else current.due_at,
                "completed" if following is None else "active",
                now,
            )
            await self._record_audit(
                conn,
                replace(current, version=current.version + 1),
                "fired",
                now,
                "system:reminders",
                notification.notification_id,
            )
            return notification

    async def notifications(
        self, tenant_id: str, user_id: str, after_id: int, limit: int
    ) -> builtins.list[NotificationRecord]:
        """按本人游标分页。"""
        async with self._pool.acquire() as conn:
            order = "DESC" if after_id == -1 else "ASC"
            rows = await conn.fetch(
                f"SELECT {NC} FROM {self._t['notification']} WHERE tenant_id=$1 AND user_id=$2 AND notification_id>$3 ORDER BY notification_id {order} LIMIT $4",
                tenant_id,
                user_id,
                after_id,
                limit,
            )
            if after_id == -1:
                rows.reverse()
        return [NotificationRecord(**dict(row)) for row in rows]

    async def read(
        self, tenant_id: str, user_id: str, notification_id: int, now: float
    ) -> NotificationRecord:
        """首个标读时间保持不变。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE {self._t['notification']} SET read_at=COALESCE(read_at,$4) WHERE tenant_id=$1 AND user_id=$2 AND notification_id=$3 RETURNING {NC}",
                tenant_id,
                user_id,
                notification_id,
                now,
            )
        if row is None:
            raise NotFoundError("通知不存在")
        return NotificationRecord(**dict(row))

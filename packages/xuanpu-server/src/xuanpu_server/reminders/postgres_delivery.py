"""提醒外部投递的 PostgreSQL 租约与身份锁。"""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.schema import DEFAULT_NAMESPACE
from xuanpu_server.errors import PermissionDeniedError
from xuanpu_server.reminders.models import DeliveryRecord, NotificationRecord
from xuanpu_server.reminders.postgres_notifications import notification_current

if TYPE_CHECKING:
    import asyncpg
    from taifeng_stores import SqlNamespace

    from xuanpu_server.controlplane.models import AuditRecord

DC = "notification_id,tenant_id,user_id,channel,status,attempts,next_attempt_at,lease_until,claim_token,error_code,first_attempt_at"
NC = "notification_id,tenant_id,user_id,reminder_id,title,due_at,created_at,read_at,notification_key,source,body,proactive_check_id,proactive_followup_id,alert_after,task_definition_id,work_task_id"


class PostgresDeliveryStore:
    """共享事务辅助和投递 outbox 存储。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名只来自校验过的命名空间。"""
        self._pool = pool
        self._t = {
            name: namespace.table(name)
            for name in (
                "tenant",
                "user",
                "reminder",
                "notification",
                "notification_delivery",
                "audit",
                "proactive_check",
                "proactive_followup",
                "workspace_item",
                "task_definition",
                "work_task", "task_dispatch",
            )
        }

    async def _identity_state(self, conn: Any, tenant_id: str, user_id: str) -> tuple[bool, bool]:
        """所有写事务统一先锁租户，再锁员工；锁内复查有效状态。"""
        tenant = await conn.fetchval(
            f"SELECT status FROM {self._t['tenant']} WHERE tenant_id=$1 FOR UPDATE", tenant_id
        )
        user = await conn.fetchval(
            f"SELECT status FROM {self._t['user']} WHERE tenant_id=$1 AND user_id=$2 FOR UPDATE",
            tenant_id,
            user_id,
        )
        return bool(tenant == "active"), bool(user == "active")

    async def _active(self, conn: Any, tenant_id: str, user_id: str) -> bool:
        """写入提醒要求租户和员工都启用。"""
        tenant, user = await self._identity_state(conn, tenant_id, user_id)
        return tenant and user

    async def _audit_event(self, conn: Any, record: AuditRecord) -> None:
        """在当前领域事务内追加审计；失败使整个事务回滚。"""
        await conn.execute(
            f"INSERT INTO {self._t['audit']} (tenant_id,user_id,source,kind,at,actor,data) VALUES ($1,$2,$3,$4,$5,$6,$7::text::jsonb)",
            record.tenant_id,
            record.user_id,
            record.source,
            record.kind,
            record.at,
            record.actor,
            json.dumps(record.data),
        )

    async def _require_active(self, conn: Any, tenant_id: str, user_id: str) -> None:
        """无有效身份拒绝写入。"""
        if not await self._active(conn, tenant_id, user_id):
            raise PermissionDeniedError("员工或租户不可用")

    async def _disable(self, conn: Any, tenant_id: str, user_id: str, now: float) -> None:
        """在身份锁内取消活动提醒和未结束投递。"""
        await conn.execute(
            f"UPDATE {self._t['reminder']} SET status='cancelled',version=version+1,updated_at=$3 WHERE tenant_id=$1 AND user_id=$2 AND status='active'",
            tenant_id,
            user_id,
            now,
        )
        await conn.execute(
            f"UPDATE {self._t['notification_delivery']} SET status='skipped',claim_token=NULL,lease_until=0,error_code='identity_disabled' WHERE tenant_id=$1 AND user_id=$2 AND status IN ('pending','sending')",
            tenant_id,
            user_id,
        )

    async def notification_current(self, record: NotificationRecord) -> bool:
        """只读取通知本人范围内的业务来源，供浮层和发送前复查复用。"""
        async with self._pool.acquire() as conn:
            return await notification_current(conn, self._t, record)

    async def claim_deliveries(self, now: float) -> list[tuple[DeliveryRecord, NotificationRecord]]:
        """扫描有界候选，每条事务先拿身份锁再拿投递锁。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {','.join('d.' + column for column in DC.split(','))} FROM {self._t['notification_delivery']} d JOIN {self._t['tenant']} t ON t.tenant_id=d.tenant_id AND t.status='active' JOIN {self._t['user']} u ON u.tenant_id=d.tenant_id AND u.user_id=d.user_id AND u.status='active' WHERE (d.status='pending' AND d.next_attempt_at<=$1) OR (d.status='sending' AND d.lease_until<=$1) ORDER BY d.next_attempt_at,d.notification_id LIMIT 100",
                now,
            )
        found = []
        for row in rows:
            item = await self._claim(DeliveryRecord(**dict(row)), now)
            if item is not None:
                found.append(item)
        return found

    async def _claim(
        self, record: DeliveryRecord, now: float
    ) -> tuple[DeliveryRecord, NotificationRecord] | None:
        """对一个候选重新检查、递增尝试并换发令牌。"""
        async with self._pool.acquire() as conn, conn.transaction():
            tenant_active, user_active = await self._identity_state(
                conn, record.tenant_id, record.user_id
            )
            if not user_active:
                await self._disable(conn, record.tenant_id, record.user_id, now)
                return None
            if not tenant_active:
                return None
            row = await conn.fetchrow(
                f"SELECT {DC} FROM {self._t['notification_delivery']} WHERE tenant_id=$1 AND user_id=$2 AND notification_id=$3 AND channel=$4 FOR UPDATE",
                record.tenant_id,
                record.user_id,
                record.notification_id,
                record.channel,
            )
            if row is None:
                return None
            current = DeliveryRecord(**dict(row))
            ready = current.status == "pending" and current.next_attempt_at <= now
            expired = current.status == "sending" and current.lease_until <= now
            if not (ready or expired):
                return None
            notification = await conn.fetchrow(
                f"SELECT {NC} FROM {self._t['notification']} WHERE tenant_id=$1 AND user_id=$2 AND notification_id=$3",
                record.tenant_id,
                record.user_id,
                record.notification_id,
            )
            assert notification is not None
            if not await notification_current(
                conn, self._t, NotificationRecord(**dict(notification))
            ):
                await conn.execute(
                    f"UPDATE {self._t['notification_delivery']} SET status='skipped',claim_token=NULL,lease_until=0,error_code='source_resolved' "
                    "WHERE tenant_id=$1 AND user_id=$2 AND notification_id=$3 AND channel=$4",
                    record.tenant_id,
                    record.user_id,
                    record.notification_id,
                    record.channel,
                )
                return None
            if current.first_attempt_at is not None and now >= current.first_attempt_at + 3600:
                await conn.execute(
                    f"UPDATE {self._t['notification_delivery']} SET status='failed',claim_token=NULL,lease_until=0,error_code='delivery_unknown' WHERE tenant_id=$1 AND user_id=$2 AND notification_id=$3 AND channel=$4",
                    record.tenant_id,
                    record.user_id,
                    record.notification_id,
                    record.channel,
                )
                return None
            if current.attempts >= 5:
                await conn.execute(
                    f"UPDATE {self._t['notification_delivery']} SET status='failed',claim_token=NULL,lease_until=0,error_code='attempts_exhausted' WHERE tenant_id=$1 AND user_id=$2 AND notification_id=$3 AND channel=$4",
                    record.tenant_id,
                    record.user_id,
                    record.notification_id,
                    record.channel,
                )
                return None
            claimed = await conn.fetchrow(
                f"UPDATE {self._t['notification_delivery']} SET status='sending',attempts=attempts+1,claim_token=$5,lease_until=$6,first_attempt_at=COALESCE(first_attempt_at,$7) WHERE tenant_id=$1 AND user_id=$2 AND notification_id=$3 AND channel=$4 RETURNING {DC}",
                record.tenant_id,
                record.user_id,
                record.notification_id,
                record.channel,
                secrets.token_hex(16),
                now + 30,
                now,
            )
            assert claimed is not None
            return DeliveryRecord(**dict(claimed)), NotificationRecord(**dict(notification))

    async def finish_delivery(
        self, record: DeliveryRecord, status: str, now: float, error_code: str | None = None
    ) -> None:
        """只接受当前未过期令牌回写，停用状态也在本事务内重新检查。"""
        async with self._pool.acquire() as conn, conn.transaction():
            _, user_active = await self._identity_state(conn, record.tenant_id, record.user_id)
            if not user_active:
                await self._disable(conn, record.tenant_id, record.user_id, now)
                return
            next_at = now + (1, 5, 30, 120)[min(record.attempts - 1, 3)]
            await conn.execute(
                f"UPDATE {self._t['notification_delivery']} SET status=$6,error_code=$7,next_attempt_at=$8,claim_token=NULL,lease_until=0,attempts=attempts-CASE WHEN $7='tenant_paused' THEN 1 ELSE 0 END,first_attempt_at=CASE WHEN $7='tenant_paused' AND attempts=1 THEN NULL ELSE first_attempt_at END WHERE tenant_id=$1 AND user_id=$2 AND notification_id=$3 AND channel=$4 AND claim_token=$5 AND status='sending' AND lease_until>$9",
                record.tenant_id,
                record.user_id,
                record.notification_id,
                record.channel,
                record.claim_token,
                status,
                error_code,
                next_at,
                now,
            )

    async def deliveries(
        self, tenant_id: str, user_id: str, notification_id: int
    ) -> list[DeliveryRecord]:
        """只读本人通知的渠道投递状态。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {DC} FROM {self._t['notification_delivery']} WHERE tenant_id=$1 AND user_id=$2 AND notification_id=$3 ORDER BY channel",
                tenant_id,
                user_id,
                notification_id,
            )
        return [DeliveryRecord(**dict(row)) for row in rows]

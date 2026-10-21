"""派生财务站内通知，不执行审批或任何外部发送。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, encode_cursor, identifier, timestamp_cursor
from xuanpu_finance.storage.f3_inbox import ComplianceStore
from xuanpu_finance.storage.f4_notice_sources import (
    OUTBOX_ACCESS_SQL,
    SOURCE_SQL,
    VISIBLE_SQL,
)

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class NotificationStore(ComplianceStore):
    """通知和人工已读状态全部持久在独立财务库。"""

    async def refresh_finance_notifications(self, *, batch_size: int = 100) -> int:
        """串行有界扫描与旧Outbox消费，持久游标保证重启后仍能轮转。"""
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise FinanceError("BATCH_SIZE_INVALID", 422, "通知批量大小须在1至1000之间")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(617472004)")
            after = await conn.fetchval(
                "SELECT after_key FROM finance_notification_scan WHERE singleton FOR UPDATE"
            )
            # Outbox和当前来源共用预算；游标轮转避免不可消费旧记录阻塞新来源。
            query = "SELECT * FROM (" + SOURCE_SQL + " UNION ALL " + self._outbox_sources() + ") s"
            rows = await conn.fetch(
                query + " WHERE scan_key>$1 ORDER BY scan_key LIMIT $2", after, batch_size
            )
            if len(rows) < batch_size:
                rows.extend(
                    await conn.fetch(
                        query + " WHERE scan_key<=$1 ORDER BY scan_key LIMIT $2",
                        after,
                        batch_size - len(rows),
                    )
                )
            inserted = 0
            for row in rows:
                if row["source_type"] == "outbox":
                    inserted += await self._record_outbox_notice(conn, row)
                else:
                    inserted += await self._insert_notice(conn, row)
            await conn.execute(
                "UPDATE finance_notification_scan SET after_key=$1 WHERE singleton",
                rows[-1]["scan_key"] if rows else "",
            )
            return inserted

    def _outbox_sources(self) -> str:
        """旧消息只取真实业务operation及原收件人，不复制消息正文。"""
        return """SELECT o.tenant_id,o.legal_entity_id,'outbox',o.id,concat(COALESCE(v.version::text,b.data->>'version','1'),':',COALESCE(v.status,b.data->>'status','recorded')),b.owner_id,
        'state_changed','财务事项状态已更新','operation',b.id,COALESCE(v.status,b.data->>'status','recorded'),
        jsonb_build_array('outbox',o.tenant_id,o.legal_entity_id,o.id,b.owner_id)::text
        FROM finance_outbox o JOIN finance_business_operation b ON(b.tenant_id=o.tenant_id AND b.legal_entity_id=o.legal_entity_id AND b.id=o.operation_id)
        LEFT JOIN finance_settlement_operation v ON(v.tenant_id=b.tenant_id AND v.legal_entity_id=b.legal_entity_id AND v.id=b.id)
        WHERE o.status='pending'"""

    async def _record_outbox_notice(self, conn: Connection, row: Any) -> int:
        """不支持来源和撤权收件人明确留诊断，真实资源才记入站内索引。"""
        operation = await conn.fetchrow(
            "SELECT * FROM finance_business_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
            row["tenant_id"],
            row["legal_entity_id"],
            row["target_id"],
        )
        assert operation is not None
        valid = await self._outbox_resource_valid(conn, operation)
        granted = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_business_operation o WHERE o.tenant_id=$1 AND o.legal_entity_id=$2 AND o.id=$3 AND "
            + OUTBOX_ACCESS_SQL
            + ")",
            row["tenant_id"],
            row["legal_entity_id"],
            row["target_id"],
        )
        status = (
            "inbox_recorded"
            if valid and granted
            else "unmapped_source"
            if not valid
            else "recipient_unavailable"
        )
        diagnostic = (
            None
            if status == "inbox_recorded"
            else "未找到可映射的真实业务资源"
            if not valid
            else "原经办当前无公司授权"
        )
        inserted = await self._insert_notice(conn, row) if status == "inbox_recorded" else 0
        await conn.execute(
            "UPDATE finance_outbox SET status=$4,inbox_diagnostic=$5 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
            row["tenant_id"],
            row["legal_entity_id"],
            row["source_id"],
            status,
            diagnostic,
        )
        return inserted

    async def _outbox_resource_valid(self, conn: Connection, operation: Any) -> bool:
        """对原operation的真实关联资源验存在性，拒绝虚构费用父单。"""
        scope = (operation["tenant_id"], operation["legal_entity_id"])
        settlement = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3)",
            *scope,
            operation["id"],
        )
        if settlement:
            return True
        resources = decode(operation["data"]).get("resources")
        if not isinstance(resources, list) or not resources:
            return False
        tables = {
            "expense_claim": "finance_expense_claim",
            "payable": "finance_expense_claim",
            "exception": "finance_exception",
            "action_plan": "finance_action_plan",
        }
        for resource in resources:
            if (
                not isinstance(resource, dict)
                or resource.get("kind") not in tables
                or not isinstance(resource.get("id"), str)
            ):
                return False
            if not await conn.fetchval(
                f"SELECT EXISTS(SELECT 1 FROM {tables[resource['kind']]} WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3)",
                *scope,
                resource["id"],
            ):
                return False
        return True

    async def _insert_notice(self, conn: Connection, source: Any) -> int:
        """唯一来源键在数据库内抵御并发、重启与重复扫描。"""
        result = await conn.fetchval(
            "INSERT INTO finance_notification(tenant_id,legal_entity_id,id,recipient_id,source_type,source_id,source_revision,source_status,kind,title,target_type,target_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) ON CONFLICT DO NOTHING RETURNING id",
            source["tenant_id"],
            source["legal_entity_id"],
            "f4notice_" + identifier(),
            source["recipient_id"],
            source["source_type"],
            source["source_id"],
            source["source_revision"],
            source["source_status"],
            source["kind"],
            source["title"],
            source["target_type"],
            source["target_id"],
        )
        return int(result is not None)

    async def list_finance_notifications(
        self,
        who: FinancePrincipal,
        entity: str,
        cursor: str | None = None,
        limit: int = 50,
        unread_only: bool = False,
    ) -> dict[str, Any]:
        """按当前公司授权和真实来源过滤后，在SQL内计数和分页。"""
        scope = request_digest([who.tenant_id, who.user_id, entity, "notifications", unread_only])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            await self._role(conn, who, entity)
            args = (who.tenant_id, entity, who.user_id)
            counts = await conn.fetchrow(
                "SELECT count(*) AS total,count(*) FILTER(WHERE n.read_at IS NULL) AS unread FROM finance_notification n WHERE "
                + VISIBLE_SQL,
                *args,
            )
            assert counts is not None
            where = VISIBLE_SQL + (" AND n.read_at IS NULL" if unread_only else "")
            rows = await conn.fetch(
                "SELECT n.* FROM finance_notification n WHERE "
                + where
                + " AND ($4::timestamptz IS NULL OR (n.created_at,n.id)>($4,$5::text)) ORDER BY n.created_at,n.id LIMIT $6",
                *args,
                stamp,
                last,
                limit + 1,
            )
            return {
                "items": [self._notice_view(r) for r in rows[:limit]],
                "total": counts["unread"] if unread_only else counts["total"],
                "unread_total": counts["unread"],
                "next_cursor": encode_cursor(
                    scope, [rows[limit - 1]["created_at"].isoformat(), rows[limit - 1]["id"]]
                )
                if len(rows) > limit
                else None,
            }

    async def read_finance_notification(
        self, who: FinancePrincipal, id: str, expected_version: int, key: str
    ) -> dict[str, Any]:
        """真人已读执行CAS和业务幂等，重试仍先复核当前来源权限。"""
        if who.origin != "human":
            raise FinanceError("HUMAN_REQUIRED", 403, "通知已读必须由真人决定")
        async with self.pool.acquire() as conn, conn.transaction():
            entity = await conn.fetchval(
                "SELECT legal_entity_id FROM finance_notification WHERE tenant_id=$1 AND recipient_id=$2 AND id=$3",
                who.tenant_id,
                who.user_id,
                id,
            )
            if entity is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "通知不存在")
            body = {"id": id, "expected_version": expected_version}
            previous = await self._business_previous(
                conn, who, entity, "readFinanceNotification", key, body
            )
            row = await conn.fetchrow(
                "SELECT n.* FROM finance_notification n WHERE "
                + VISIBLE_SQL
                + " AND n.id=$4 FOR UPDATE OF n",
                who.tenant_id,
                entity,
                who.user_id,
                id,
            )
            if row is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "通知不存在")
            if previous is not None:
                return previous
            if row["version"] != expected_version:
                raise FinanceError("VERSION_CONFLICT", 409, "通知版本已变化")
            row = await conn.fetchrow(
                "UPDATE finance_notification SET read_at=COALESCE(read_at,now()),version=version+1 WHERE tenant_id=$1 AND id=$2 RETURNING *",
                who.tenant_id,
                id,
            )
            return await self._business_remember(
                conn, who, entity, "readFinanceNotification", key, body, self._notice_view(row)
            )

    def _notice_view(self, row: Any) -> dict[str, Any]:
        """固定公开字段，只包含少敏中文标题和原目标引用。"""
        fields = (
            "id",
            "legal_entity_id",
            "version",
            "kind",
            "title",
            "target_type",
            "target_id",
            "source_status",
        )
        result = {field: row[field] for field in fields}
        result.update(
            {
                field: row[field].isoformat() if row[field] is not None else None
                for field in ("created_at", "read_at")
            }
        )
        return result

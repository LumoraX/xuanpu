"""按公司及记录所有者隔离、使用稳定键集游标的只读接口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import (
    FinanceBase,
    decode,
    encode_cursor,
    page,
    position_cursor,
    timestamp_cursor,
)

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal


class ReadStore(FinanceBase):
    """只查询本服务数据，不通过跨库关系隐式扩大权限。"""

    async def list_companies(
        self, who: FinancePrincipal, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """按接收时间及标识列出本人获得授权的公司，新增不扰动已读位置。"""
        scope = request_digest([who.tenant_id, who.user_id, "companies"])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            total = await conn.fetchval(
                "SELECT count(*) FROM finance_company c JOIN finance_grant g USING(tenant_id,legal_entity_id) WHERE c.tenant_id=$1 AND g.user_id=$2",
                who.tenant_id,
                who.user_id,
            )
            rows = await conn.fetch(
                "SELECT c.legal_entity_id AS id,c.name,c.tax_id,c.received_at FROM finance_company c JOIN finance_grant g USING(tenant_id,legal_entity_id) WHERE c.tenant_id=$1 AND g.user_id=$2 AND ($3::timestamptz IS NULL OR (c.received_at,c.legal_entity_id)>($3,$4::text)) ORDER BY c.received_at,c.legal_entity_id LIMIT $5",
                who.tenant_id,
                who.user_id,
                stamp,
                last,
                limit + 1,
            )
            visible = rows[:limit]
            next_cursor = (
                encode_cursor(scope, [visible[-1]["received_at"].isoformat(), visible[-1]["id"]])
                if len(rows) > limit
                else None
            )
            return page(
                [{name: row[name] for name in ("id", "name", "tax_id")} for row in visible],
                total,
                next_cursor,
            )

    async def list_integrations(self, who: FinancePrincipal, entity: str) -> dict[str, Any]:
        """明确显示 F0 未配置的外部能力。"""
        async with self.pool.acquire() as conn:
            await self._role(conn, who, entity)
        return {
            "legal_entity_id": entity,
            "items": [
                {
                    "id": f"{entity}:{kind}",
                    "kind": kind,
                    "status": "unconfigured",
                    "capabilities": [],
                }
                for kind in ("erp", "payment", "tax", "archive", "approval")
            ],
        }

    async def list_invoices(
        self,
        who: FinancePrincipal,
        entity: str,
        cursor: str | None = None,
        limit: int = 50,
        number: str | None = None,
    ) -> dict[str, Any]:
        """本人或 reviewer 公司范围内按接收时间、标识续读票据。"""
        scope = request_digest([who.tenant_id, who.user_id, "invoices", entity, number])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            role = await self._role(conn, who, entity)
            args = (who.tenant_id, entity, who.user_id, role == "reviewer", number)
            total = await conn.fetchval(
                "SELECT count(*) FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND (owner_id=$3 OR $4) AND ($5::text IS NULL OR number=$5)",
                *args,
            )
            rows = await conn.fetch(
                "SELECT data,received_at,id FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND (owner_id=$3 OR $4) AND ($5::text IS NULL OR number=$5) AND ($6::timestamptz IS NULL OR (received_at,id)>($6,$7::text)) ORDER BY received_at,id LIMIT $8",
                *args,
                stamp,
                last,
                limit + 1,
            )
            visible = rows[:limit]
            next_cursor = (
                encode_cursor(scope, [visible[-1]["received_at"].isoformat(), visible[-1]["id"]])
                if len(rows) > limit
                else None
            )
            return page([decode(row["data"]) for row in visible], total, next_cursor)

    async def get_invoice(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """读取本人或授权 reviewer 可见的票面快照。"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM finance_invoice WHERE tenant_id=$1 AND id=$2", who.tenant_id, id
            )
            row = await self._authorize(conn, who, row)
            return decode(row["data"])

    async def list_invoice_lines(
        self, who: FinancePrincipal, id: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """不可变明细按持久位置续读，total 独立于当前页。"""
        return await self._invoice_children(who, id, "lines", cursor, limit)

    async def list_invoice_fields(
        self, who: FinancePrincipal, id: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """不可变字段按持久位置续读，逐项保留来源回执。"""
        return await self._invoice_children(who, id, "fields", cursor, limit)

    async def _invoice_children(
        self, who: FinancePrincipal, id: str, kind: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        """通过固定 SQL 对已授权票据的明细或字段做键集分页。"""
        scope = request_digest([who.tenant_id, who.user_id, kind, id])
        position = position_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            row = await conn.fetchrow(
                "SELECT * FROM finance_invoice WHERE tenant_id=$1 AND id=$2", who.tenant_id, id
            )
            row = await self._authorize(conn, who, row)
            args = (who.tenant_id, row["legal_entity_id"], id)
            if kind == "lines":
                total = await conn.fetchval(
                    "SELECT count(*) FROM finance_invoice_line WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3",
                    *args,
                )
                rows = await conn.fetch(
                    "SELECT data,position FROM finance_invoice_line WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 AND position>$4 ORDER BY position LIMIT $5",
                    *args,
                    position,
                    limit + 1,
                )
            else:
                total = await conn.fetchval(
                    "SELECT count(*) FROM finance_invoice_field WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3",
                    *args,
                )
                rows = await conn.fetch(
                    "SELECT data,position FROM finance_invoice_field WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 AND position>$4 ORDER BY position LIMIT $5",
                    *args,
                    position,
                    limit + 1,
                )
            visible = rows[:limit]
            next_cursor = (
                encode_cursor(scope, [str(visible[-1]["position"])]) if len(rows) > limit else None
            )
            return page([decode(record["data"]) for record in visible], total, next_cursor)

    async def get_invoice_evidence(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """原件证据保全不等于官方查验，不以摘要一致提升验证状态。"""
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            row = await conn.fetchrow(
                "SELECT * FROM finance_invoice WHERE tenant_id=$1 AND id=$2", who.tenant_id, id
            )
            row = await self._authorize(conn, who, row)
            records = await conn.fetch(
                "SELECT r.* FROM finance_receipt r JOIN finance_invoice_receipt l USING(tenant_id,legal_entity_id) WHERE l.invoice_id=$3 AND l.receipt_id=r.id AND r.tenant_id=$1 AND r.legal_entity_id=$2 ORDER BY r.id LIMIT 1000",
                who.tenant_id,
                row["legal_entity_id"],
                id,
            )
            evidence = [
                {
                    "id": record["id"],
                    "kind": "source",
                    "sha256": record["sha256"],
                    "verification_state": "not_checked",
                    "source_version": str(decode(record["file_ref"])["revision"]),
                }
                for record in records
            ]
            return {
                "invoice_id": id,
                "evidence": evidence,
                "gaps": ["尚未配置官方查验", "尚未配置归档"],
            }

    async def list_exceptions(
        self, who: FinancePrincipal, entity: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """按接收时间及标识续读持久异常，不隐藏失败及已解决记录。"""
        scope = request_digest([who.tenant_id, who.user_id, "exceptions", entity])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            role = await self._role(conn, who, entity)
            args = (who.tenant_id, entity, who.user_id, role == "reviewer")
            total = await conn.fetchval(
                "SELECT count(*) FROM finance_exception WHERE tenant_id=$1 AND legal_entity_id=$2 AND (owner_id=$3 OR $4)",
                *args,
            )
            rows = await conn.fetch(
                "SELECT e.*,r.sha256 FROM finance_exception e LEFT JOIN finance_receipt r ON (r.tenant_id=e.tenant_id AND r.legal_entity_id=e.legal_entity_id AND r.id=e.receipt_id) WHERE e.tenant_id=$1 AND e.legal_entity_id=$2 AND (e.owner_id=$3 OR $4) AND ($5::timestamptz IS NULL OR (e.received_at,e.id)>($5,$6::text)) ORDER BY e.received_at,e.id LIMIT $7",
                *args,
                stamp,
                last,
                limit + 1,
            )
            visible = rows[:limit]
            items = [
                {
                    "id": record["id"],
                    "version": record["version"],
                    "legal_entity_id": entity,
                    "rule_id": record["rule_id"],
                    "severity": "hard_block",
                    "status": record["status"],
                    "evidence": [
                        {
                            "id": record["receipt_id"],
                            "kind": "source",
                            "sha256": record["sha256"],
                            "verification_state": "not_checked",
                        }
                    ]
                    if record["receipt_id"]
                    else [],
                }
                for record in visible
            ]
            next_cursor = (
                encode_cursor(scope, [visible[-1]["received_at"].isoformat(), visible[-1]["id"]])
                if len(rows) > limit
                else None
            )
            return page(items, total, next_cursor)

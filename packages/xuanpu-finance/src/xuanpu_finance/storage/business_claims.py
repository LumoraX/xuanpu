"""报销草稿的本人写入、不可变修订及授权查询。"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.business_catalog import CatalogStore
from xuanpu_finance.storage.common import encode_cursor, identifier, page, timestamp_cursor

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ExpenseDraft, ExpenseUpdate
    from xuanpu_finance.models import FinancePrincipal


class ClaimStore(CatalogStore):
    """报销事实持有于独立业务库，平台无需持有单据状态。"""

    async def create_expense_claim(
        self, who: FinancePrincipal, body: ExpenseDraft, key: str
    ) -> dict[str, Any]:
        """仅以验证后的登录身份创建本人草稿，总额服务端计算。"""
        entity = body.legal_entity_id
        request = body.model_dump(mode="json", exclude_none=True)
        async with self.pool.acquire() as conn, conn.transaction():
            previous = await self._business_previous(
                conn, who, entity, "createExpenseClaim", key, request
            )
            if previous is not None:
                return previous
            policy = await self._policy(conn, who.tenant_id, entity)
            assert policy is not None
            self._validate_draft(who, body, policy)
            await self._validate_receipts(conn, who, entity, body.evidence_ids)
            total = sum((Decimal(line.amount) for line in body.lines), Decimal(0))
            row = await conn.fetchrow(
                "INSERT INTO finance_expense_claim(tenant_id,legal_entity_id,id,owner_id,version,status,total,content,record_mode,simulation) VALUES($1,$2,$3,$4,1,'draft',$5,$6::jsonb,$7,COALESCE((SELECT simulation FROM finance_settlement_mode WHERE singleton),false)) RETURNING *",
                who.tenant_id,
                entity,
                identifier(),
                who.user_id,
                total,
                json.dumps(request),
                policy["record_mode"],
            )
            await self._link_receipts(conn, row, body.evidence_ids)
            result = await self._save_history(conn, row)
            await self._event(conn, row, "expense.created", {"version": 1})
            return await self._business_remember(
                conn, who, entity, "createExpenseClaim", key, request, result
            )

    async def update_expense_claim(
        self, who: FinancePrincipal, id: str, body: ExpenseUpdate, key: str
    ) -> dict[str, Any]:
        """完整修订本人草稿并释放旧proposal，不覆盖历史。"""
        request = {"id": id, **body.model_dump(mode="json", exclude_none=True)}
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._claim_row(conn, who, id, lock=True)
            entity = row["legal_entity_id"]
            previous = await self._business_previous(
                conn, who, entity, "updateExpenseClaim", key, request
            )
            if previous is not None:
                return previous
            if row["document_kind"] != "expense_claim":
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "报销不存在")
            self._owner_edit(who, row, body.expected_version)
            if body.draft.legal_entity_id != entity:
                raise FinanceError("COMPANY_IMMUTABLE", 422, "报销公司不能更换")
            policy = await self._policy(conn, who.tenant_id, entity)
            assert policy is not None
            self._validate_draft(who, body.draft, policy)
            await conn.execute(
                "UPDATE finance_allocation SET state='released',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state='proposed'",
                who.tenant_id,
                entity,
                id,
            )
            await self._validate_receipts(conn, who, entity, body.draft.evidence_ids)
            total = sum((Decimal(line.amount) for line in body.draft.lines), Decimal(0))
            row = await conn.fetchrow(
                "UPDATE finance_expense_claim SET version=version+1,status='draft',total=$4,content=$5::jsonb,record_mode=$6 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *",
                who.tenant_id,
                entity,
                id,
                total,
                body.draft.model_dump_json(exclude_none=True),
                policy["record_mode"],
            )
            assert row is not None
            await self._link_receipts(conn, row, body.draft.evidence_ids)
            result = await self._save_history(conn, row)
            await self._event(conn, row, "expense.revised", {"version": row["version"]})
            return await self._business_remember(
                conn, who, entity, "updateExpenseClaim", key, request, result
            )

    async def get_expense_claim(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """本人、当前reviewer和历史实际分派者可恢复同一单据。"""
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            row = await self._claim_row(conn, who, id)
            if row["document_kind"] != "expense_claim":
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "报销不存在")
            return await self._claim_snapshot(conn, row)

    async def list_expense_claims(
        self,
        who: FinancePrincipal,
        entity: str,
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
    ) -> dict[str, Any]:
        """按可见范围、状态独立计数，并用创建时间与ID稳定续读。"""
        if status not in {
            None,
            "draft",
            "submitted",
            "under_review",
            "approved",
            "rejected",
            "withdrawn",
        }:
            raise FinanceError("CLAIM_STATUS_INVALID", 422, "报销状态无效")
        scope = request_digest([who.tenant_id, who.user_id, entity, "expense_claims", status])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            role = await self._role(conn, who, entity)
            where = "c.document_kind='expense_claim' AND c.tenant_id=$1 AND c.legal_entity_id=$2 AND (c.owner_id=$3 OR $4::text='reviewer' OR ($4::text='approver' AND EXISTS(SELECT 1 FROM finance_workflow w JOIN finance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) WHERE w.tenant_id=c.tenant_id AND w.legal_entity_id=c.legal_entity_id AND w.document_id=c.id AND s.approver_user_id=$3))) AND ($5::text IS NULL OR c.status=$5)"
            args = (who.tenant_id, entity, who.user_id, role, status)
            total = await conn.fetchval(
                "SELECT count(*) FROM finance_expense_claim c WHERE " + where, *args
            )
            rows = await conn.fetch(
                "SELECT c.* FROM finance_expense_claim c WHERE "
                + where
                + " AND ($6::timestamptz IS NULL OR (c.created_at,c.id)>($6,$7::text)) ORDER BY c.created_at,c.id LIMIT $8",
                *args,
                stamp,
                last,
                limit + 1,
            )
            visible = rows[:limit]
            cursor_next = (
                encode_cursor(scope, [visible[-1]["created_at"].isoformat(), visible[-1]["id"]])
                if len(rows) > limit
                else None
            )
            return page(
                [await self._claim_snapshot(conn, row) for row in visible], total, cursor_next
            )

"""原生应付与报销共享聚合、分票和业务审批，不重复资金占额。"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import PayableDraft
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.business_base import money
from xuanpu_finance.storage.common import decode, encode_cursor, identifier, page, timestamp_cursor
from xuanpu_finance.storage.f2_catalog import SettlementCatalogStore

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ActionPreview, PayableUpdate
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


INFLIGHT = ("accepted", "running", "dispatching", "awaiting_provider", "outcome_unknown")


class SettlementDocumentStore(SettlementCatalogStore):
    """原生应付与报销使用相同隔离、历史与发票额度事务。"""

    async def _validate_payable(
        self, conn: Connection, who: FinancePrincipal, draft: PayableDraft
    ) -> None:
        """供应商和采购依据精确绑定当前可见的实际来源快照。"""
        for ref in [draft.supplier_ref, *draft.business_refs]:
            data = await self._catalog_ref(conn, who, draft.legal_entity_id, ref)
            if data["verification_state"] != "verified":
                raise FinanceError("BUSINESS_DOCUMENT_NOT_VERIFIED", 409, "采购依据尚未核实")
        await self._validate_receipts(conn, who, draft.legal_entity_id, draft.evidence_ids)

    async def create_payable(
        self, who: FinancePrincipal, body: PayableDraft, key: str
    ) -> dict[str, Any]:
        """本人创建应付草稿，身份、环境和状态由服务决定。"""
        request = body.model_dump(mode="json", exclude_none=True)
        async with self.pool.acquire() as conn, conn.transaction():
            old = await self._business_previous(
                conn, who, body.legal_entity_id, "createPayable", key, request
            )
            if old is not None:
                return old
            await self._policy(conn, who.tenant_id, body.legal_entity_id)
            await self._validate_payable(conn, who, body)
            row = await conn.fetchrow(
                "INSERT INTO finance_expense_claim(tenant_id,legal_entity_id,id,owner_id,version,status,total,content,record_mode,document_kind,simulation) VALUES($1,$2,$3,$4,1,'draft',$5,$6::jsonb,'invoice_backed','payable',$7) RETURNING *",
                who.tenant_id,
                body.legal_entity_id,
                identifier(),
                who.user_id,
                Decimal(body.amount),
                json.dumps(request),
                await self._simulation(conn),
            )
            await self._link_receipts(conn, row, body.evidence_ids)
            result = await self._save_history(conn, row)
            await self._event(conn, row, "payable.created", {"version": 1})
            return await self._business_remember(
                conn, who, body.legal_entity_id, "createPayable", key, request, result
            )

    async def _payable_row(
        self, conn: Connection, who: FinancePrincipal, id: str, *, lock: bool = False
    ) -> Any:
        """应付入口不接受同一底表中的报销ID。"""
        row = await self._claim_row(conn, who, id, lock=lock)
        if row["document_kind"] != "payable":
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "应付不存在")
        return row

    async def update_payable(
        self, who: FinancePrincipal, id: str, body: PayableUpdate, key: str
    ) -> dict[str, Any]:
        """CAS修订保留旧版本、原件和分配历史，不能更换环境。"""
        request = {"id": id, **body.model_dump(mode="json", exclude_none=True)}
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._payable_row(conn, who, id, lock=True)
            old = await self._business_previous(
                conn, who, row["legal_entity_id"], "updatePayable", key, request
            )
            if old is not None:
                return old
            self._owner_edit(who, row, body.expected_version)
            await self._settlement_withdraw_guard(conn, row)
            if body.draft.legal_entity_id != row["legal_entity_id"]:
                raise FinanceError("COMPANY_IMMUTABLE", 422, "应付公司不可更换")
            await self._validate_payable(conn, who, body.draft)
            await conn.execute(
                "UPDATE finance_allocation SET state='released',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state='proposed'",
                who.tenant_id,
                row["legal_entity_id"],
                id,
            )
            row = await conn.fetchrow(
                "UPDATE finance_expense_claim SET total=$4,content=$5::jsonb,version=version+1,status='draft' WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *",
                who.tenant_id,
                row["legal_entity_id"],
                id,
                Decimal(body.draft.amount),
                body.draft.model_dump_json(exclude_none=True),
            )
            assert row is not None
            await self._link_receipts(conn, row, body.draft.evidence_ids)
            result = await self._save_history(conn, row)
            await self._event(conn, row, "payable.revised", {"version": row["version"]})
            return await self._business_remember(
                conn, who, row["legal_entity_id"], "updatePayable", key, request, result
            )

    async def get_payable(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """按单据实际权限恢复应付、业务批准和独立资金结果。"""
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            return await self._claim_snapshot(conn, await self._payable_row(conn, who, id))

    async def list_payables(
        self,
        who: FinancePrincipal,
        entity: str,
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
    ) -> dict[str, Any]:
        """应付过滤权限和状态后计算total，稳定续读。"""
        if status not in {
            None,
            "draft",
            "submitted",
            "under_review",
            "approved",
            "rejected",
            "withdrawn",
        }:
            raise FinanceError("PAYABLE_STATUS_INVALID", 422, "应付状态无效")
        scope = request_digest([who.tenant_id, who.user_id, entity, "payables", status])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            role = await self._role(conn, who, entity)
            where = "c.tenant_id=$1 AND c.legal_entity_id=$2 AND c.document_kind='payable' AND (c.owner_id=$3 OR $4::text='reviewer' OR ($4::text='approver' AND EXISTS(SELECT 1 FROM finance_workflow w JOIN finance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) WHERE w.tenant_id=c.tenant_id AND w.legal_entity_id=c.legal_entity_id AND w.document_id=c.id AND s.approver_user_id=$3))) AND ($5::text IS NULL OR c.status=$5)"
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
            return page(
                [await self._claim_snapshot(conn, row) for row in visible],
                total,
                encode_cursor(scope, [visible[-1]["created_at"].isoformat(), visible[-1]["id"]])
                if len(rows) > limit
                else None,
            )

    async def _claim_snapshot(self, conn: Connection, row: Any) -> dict[str, Any]:
        """单据版本与付款、记账的独立结果版本分开投影。"""
        result = await super()._claim_snapshot(conn, row)
        result.update({"kind": row["document_kind"], "simulation": row["simulation"]})
        result.update(await self._settlement_totals(conn, row))
        return result

    async def _settlement_totals(
        self, conn: Connection, row: Any, exclude_operation_id: str | None = None
    ) -> dict[str, Any]:
        """付款与记账分别汇总，未知及所有在途状态保留金额占用。"""
        operations = await conn.fetch(
            "SELECT action,amount,status FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND ($4::text IS NULL OR id<>$4)",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
            exclude_operation_id,
        )

        def total(action: str, states: set[str]) -> Decimal:
            """按动作和明确状态求和，不使用HTTP结果推断业务。"""
            return sum(
                (
                    op["amount"]
                    for op in operations
                    if op["action"] == action and op["status"] in states
                ),
                Decimal(0),
            )

        paid = total("execute_payment", {"succeeded"})
        payment_pending = total("execute_payment", set(INFLIGHT))
        posted = total("submit_posting", {"succeeded"})
        reversed_amount = total("reverse_posting", {"succeeded"})
        posting_pending = total("submit_posting", set(INFLIGHT))
        unknown = any(
            op["action"] == "execute_payment" and op["status"] == "outcome_unknown"
            for op in operations
        )
        payment_status = (
            "outcome_unknown"
            if unknown
            else "paid"
            if paid == row["total"]
            else "partially_paid"
            if paid
            else "pending"
            if payment_pending
            else "not_requested"
        )
        posting_status = (
            "reversed"
            if reversed_amount and reversed_amount == posted
            else "partially_reversed"
            if reversed_amount
            else "posted"
            if posted == row["total"]
            else "partially_posted"
            if posted
            else "not_posted"
        )
        return {
            "payment_status": payment_status,
            "paid_amount": money(paid),
            "payment_pending_amount": money(payment_pending),
            "posting_status": posting_status,
            "posted_amount": money(posted - reversed_amount),
            "reversed_amount": money(reversed_amount),
            "posting_pending_amount": money(posting_pending),
        }

    async def _settlement_withdraw_guard(self, conn: Connection, row: Any) -> None:
        """已成功、已发送或未知外部动作不能通过撤回释放发票占额。"""
        blocked = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND (status IN ('dispatching','awaiting_provider','outcome_unknown','succeeded') OR request IS NOT NULL))",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
        )
        if blocked:
            raise FinanceError(
                "EXTERNAL_EFFECT_PREVENTS_WITHDRAW",
                409,
                "存在已发送、未知或已成功外部动作，不能撤回或释放占额",
            )
        cancelled = await conn.fetch(
            "UPDATE finance_settlement_operation SET status='cancelled',version=version+1,lease_token=NULL,lease_until=NULL WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND status IN ('accepted','running') RETURNING *",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
        )
        for operation in cancelled:
            await self._update_record_state(conn, operation, "cancelled", {})
            await self._event(
                conn, row, "settlement.cancelled_by_withdraw", {"operation_id": operation["id"]}
            )
            await self._settlement_outbox(
                conn, operation, self._settlement_operation_view(operation)
            )
        await conn.execute(
            "UPDATE finance_execution_grant SET active=false WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
        )

    async def _validate_action(
        self,
        conn: Connection,
        who: FinancePrincipal,
        claim: Any,
        body: ActionPreview,
        *,
        lock: bool,
    ) -> list[str]:
        """应付提交仅替换草稿检查，其余复用F1顺序审批和占额。"""
        if (
            body.target_type not in {"action_plan", "posting"}
            and body.target_type != claim["document_kind"]
        ):
            raise FinanceError("TARGET_KIND_MISMATCH", 422, "动作目标类型不符")
        if body.action == "submit_payable":
            self._owner_edit(who, claim, body.expected_version)
            await self._validate_payable(conn, who, PayableDraft(**decode(claim["content"])))
            policy = await self._policy(conn, who.tenant_id, claim["legal_entity_id"])
            assert policy is not None
            steps = await self._applicable_steps(conn, claim, policy)
            await self._reservation_check(conn, claim, policy, lock=lock)
            return [step["approver_user_id"] for step in steps]
        if body.action == "withdraw":
            if lock:
                await self._settlement_withdraw_guard(conn, claim)
            else:
                await self._withdraw_read_check(conn, claim)
        return await super()._validate_action(conn, who, claim, body, lock=lock)

    async def _withdraw_read_check(self, conn: Connection, claim: Any) -> None:
        """只读预检不取消未发送任务，也绝不释放任何额度。"""
        blocked = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND (status IN ('dispatching','awaiting_provider','outcome_unknown','succeeded') OR request IS NOT NULL))",
            claim["tenant_id"],
            claim["legal_entity_id"],
            claim["id"],
        )
        if blocked:
            raise FinanceError(
                "EXTERNAL_EFFECT_PREVENTS_WITHDRAW", 409, "存在外部执行事实，不能撤回"
            )

    async def _apply_action(
        self, conn: Connection, who: FinancePrincipal, claim: Any, payload: dict[str, Any]
    ) -> None:
        """应付提交复用F1事务和分配，后续决定不复制实现。"""
        if payload["action"] == "submit_payable":
            await self._submit_claim(conn, who, claim)
        else:
            await super()._apply_action(conn, who, claim, payload)

    async def list_payee_accounts(
        self,
        who: FinancePrincipal,
        entity: str,
        document_kind: str,
        document_id: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """只返回指定单据真实主体允许的已核实脱敏账户。"""
        scope = request_digest(
            [who.tenant_id, who.user_id, entity, "payees", document_kind, document_id]
        )
        from xuanpu_finance.storage.common import decode_cursor

        after = decode_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction():
            doc = await self._claim_row(conn, who, document_id)
            if doc["legal_entity_id"] != entity or doc["document_kind"] != document_kind:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "单据不存在")
            kind = "employee_account" if document_kind == "expense_claim" else "supplier_account"
            subject = (
                doc["owner_id"]
                if document_kind == "expense_claim"
                else decode(doc["content"])["supplier_ref"]["id"]
            )
            rows = await conn.fetch(
                "SELECT r.data FROM finance_settlement_catalog c JOIN finance_settlement_catalog_revision r USING(tenant_id,legal_entity_id,kind,source,id,version) WHERE c.tenant_id=$1 AND c.legal_entity_id=$2 AND c.kind=$3 AND c.enabled ORDER BY c.source,c.id",
                who.tenant_id,
                entity,
                kind,
            )
            items = [decode(row["data"]) for row in rows]
            items = [
                d
                for d in items
                if d["verified"]
                and d["subject_id"] == subject
                and who.user_id in d["allowed_user_ids"]
            ]
            remaining = [
                d for d in items if after is None or [d["ref"]["source"], d["ref"]["id"]] > after
            ]
            visible = remaining[:limit]
            return page(
                [
                    {k: d[k] for k in ("ref", "account_name", "masked_account", "verified")}
                    for d in visible
                ],
                len(items),
                encode_cursor(scope, [visible[-1]["ref"]["source"], visible[-1]["ref"]["id"]])
                if len(remaining) > limit
                else None,
            )

    def _settlement_operation_view(self, row: Any) -> dict[str, Any]:
        """操作受理和外部业务结果独立，未知仍保留可核对引用。"""
        status = row["status"]
        return {
            "id": row["id"],
            "version": row["version"],
            "status": status,
            "business_result": "succeeded"
            if status == "succeeded"
            else "failed"
            if status == "failed"
            else "not_completed",
            "trace_id": row["id"],
            "items": [],
            "result_refs": [],
            "resources": [{"kind": row["document_kind"], "id": row["document_id"]}],
            "action": row["action"],
            "simulation": row["simulation"],
            "provider": row["provider"],
            "external_request_id": row["external_request_id"],
            "plan_id": row["plan_id"],
        }

    async def _settlement_outbox(self, conn: Connection, row: Any, data: dict[str, Any]) -> None:
        """独立外部队列通知仍保留pending，不冒称已经送达。"""
        await conn.execute(
            "INSERT INTO finance_business_operation(tenant_id,legal_entity_id,id,owner_id,plan_id,data) VALUES($1,$2,$3,$4,NULL,$5::jsonb) ON CONFLICT(tenant_id,legal_entity_id,id) DO NOTHING",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
            row["owner_id"],
            json.dumps(data),
        )
        await conn.execute(
            "INSERT INTO finance_outbox(tenant_id,legal_entity_id,id,operation_id,data) VALUES($1,$2,$3,$4,$5::jsonb)",
            row["tenant_id"],
            row["legal_entity_id"],
            identifier(),
            row["id"],
            json.dumps(data),
        )

    async def _update_record_state(
        self, conn: Connection, row: Any, status: str, result: dict[str, Any]
    ) -> None:
        """只更新记录独立版本和渠道事实，不修改单据内容版本。"""
        if status in {"accepted", "running"}:
            status = "pending"
        table = "finance_payment" if row["action"] == "execute_payment" else "finance_posting"
        fields = (
            {"external_order_id", "transaction_id", "evidence"}
            if table == "finance_payment"
            else {"ledger_id", "accounting_period", "voucher_id", "evidence"}
        )
        data = {k: v for k, v in result.items() if k in fields} if status == "succeeded" else {}
        await conn.execute(
            f"UPDATE {table} SET status=$4,version=version+1,data=data||$5::jsonb WHERE tenant_id=$1 AND legal_entity_id=$2 AND operation_id=$3",
            row["tenant_id"],
            row["legal_entity_id"],
            row["id"],
            status,
            json.dumps(data),
        )

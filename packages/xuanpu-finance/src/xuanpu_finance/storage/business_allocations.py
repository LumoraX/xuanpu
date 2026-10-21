"""发票提议分配与排序锁下的金额、税额和行累计复核。"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.business_base import money
from xuanpu_finance.storage.business_claims import ClaimStore
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f3_projection import (
    effective_line_limits,
    invoice_projection,
    require_usable_projection,
)

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import AllocationRequest
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


def allocation_snapshot(row: Any) -> dict[str, Any]:
    """对外分配快照不把提议误称预留。"""
    return {
        "id": row["id"],
        "version": row["version"],
        "invoice_id": row["invoice_id"],
        "document_id": row["document_id"],
        "amount": money(row["amount"]),
        "tax": money(row["tax"]),
        "state": row["state"],
    }


class AllocationStore(ClaimStore):
    """所有额度只在提交的同一事务内原子预留。"""

    async def create_invoice_allocation(
        self, who: FinancePrincipal, body: AllocationRequest, key: str
    ) -> dict[str, Any]:
        """创建不占额proposal，同时令旧单据计划失效。"""
        request = body.model_dump(mode="json", exclude_none=True)
        async with self.pool.acquire() as conn, conn.transaction():
            claim = await self._claim_row(conn, who, body.document_id, lock=True)
            if body.document_kind != claim["document_kind"]:
                raise FinanceError("TARGET_KIND_MISMATCH", 422, "分配单据类型不符")
            entity = claim["legal_entity_id"]
            previous = await self._business_previous(
                conn, who, entity, "createInvoiceAllocation", key, request
            )
            if previous is not None:
                return previous
            self._owner_edit(who, claim, body.expected_document_version)
            await self._policy(conn, who.tenant_id, entity)
            invoice = await conn.fetchrow(
                "SELECT * FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR SHARE",
                who.tenant_id,
                entity,
                body.invoice_id,
            )
            data = await self._allocation_invoice(conn, claim, invoice, body.invoice_version)
            if Decimal(body.amount) > Decimal(data["effective_gross"]) or Decimal(body.tax) > Decimal(
                data["effective_tax"]
            ):
                raise FinanceError("ALLOCATION_LIMIT", 409, "分配超过票面金额或税额")
            if body.invoice_line_id is not None:
                await self._line_limit(
                    conn,
                    claim,
                    body.invoice_id,
                    body.invoice_line_id,
                    Decimal(body.amount),
                    Decimal(body.tax),
                )
            row = await conn.fetchrow(
                "INSERT INTO finance_allocation(tenant_id,legal_entity_id,id,document_id,invoice_id,invoice_version,invoice_line_id,amount,tax,state) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'proposed') RETURNING *",
                who.tenant_id,
                entity,
                identifier(),
                claim["id"],
                body.invoice_id,
                body.invoice_version,
                body.invoice_line_id,
                Decimal(body.amount),
                Decimal(body.tax),
            )
            claim = await self._bump_claim(conn, claim)
            await self._save_history(conn, claim)
            result = allocation_snapshot(row)
            await self._event(conn, claim, "allocation.proposed", result)
            return await self._business_remember(
                conn, who, entity, "createInvoiceAllocation", key, request, result
            )

    async def get_invoice_allocation(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """分配可见性严格继承其业务单据。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM finance_allocation WHERE tenant_id=$1 AND id=$2", who.tenant_id, id
            )
            if row is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "分配不存在")
            await self._claim_row(conn, who, row["document_id"])
            return allocation_snapshot(row)

    async def release_invoice_allocation(
        self, who: FinancePrincipal, id: str, expected_version: int, key: str
    ) -> dict[str, Any]:
        """本人仅可解除可编辑单据的proposal，reserved必须走撤回。"""
        request = {"id": id, "expected_version": expected_version}
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM finance_allocation WHERE tenant_id=$1 AND id=$2", who.tenant_id, id
            )
            if row is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "分配不存在")
            claim = await self._claim_row(conn, who, row["document_id"], lock=True)
            entity = claim["legal_entity_id"]
            previous = await self._business_previous(
                conn, who, entity, "releaseInvoiceAllocation", key, request
            )
            if previous is not None:
                return previous
            self._owner_edit(who, claim, claim["version"])
            row = await conn.fetchrow(
                "SELECT * FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE",
                who.tenant_id,
                entity,
                id,
            )
            assert row is not None
            if row["version"] != expected_version or row["state"] != "proposed":
                raise FinanceError("VERSION_CONFLICT", 409, "分配已变化或不可直接释放")
            row = await conn.fetchrow(
                "UPDATE finance_allocation SET state='released',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 RETURNING *",
                who.tenant_id,
                entity,
                id,
            )
            claim = await self._bump_claim(conn, claim)
            await self._save_history(conn, claim)
            result = allocation_snapshot(row)
            await self._event(conn, claim, "allocation.released", result)
            return await self._business_remember(
                conn, who, entity, "releaseInvoiceAllocation", key, request, result
            )

    async def _allocation_invoice(
        self, conn: Connection, claim: Any, invoice: Any, version: int
    ) -> dict[str, Any]:
        """重查同公司、本人、蓝票、正常票态和公司买方税号。"""
        if invoice is None or invoice["owner_id"] != claim["owner_id"]:
            raise FinanceError("INVOICE_FORBIDDEN", 403, "发票不存在或不属于申请人")
        data = await invoice_projection(conn, claim["tenant_id"], claim["legal_entity_id"], decode(invoice["data"]))
        require_usable_projection(data)
        tax_id = await conn.fetchval(
            "SELECT tax_id FROM finance_company WHERE tenant_id=$1 AND legal_entity_id=$2 FOR SHARE",
            claim["tenant_id"],
            claim["legal_entity_id"],
        )
        if data["version"] != version:
            raise FinanceError("VERSION_CONFLICT", 409, "发票版本已变化")
        if data["is_red"] or data["ticket_status"] not in {"normal", "partially_red"} or Decimal(data["effective_gross"]) <= 0:
            raise FinanceError("INVOICE_NOT_USABLE", 409, "仅正常正金额蓝票可用于报销")
        if data["buyer_tax_id"] != tax_id:
            raise FinanceError("INVOICE_BUYER_MISMATCH", 409, "发票买方税号与公司不符")
        if data["verification_status"] == "failed":
            raise FinanceError("INVOICE_VERIFICATION_FAILED", 409, "发票查验失败不能用于报销")
        hard = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_exception e WHERE e.tenant_id=$1 AND e.legal_entity_id=$2 AND e.severity='hard_block' AND e.status NOT IN ('resolved','accepted') AND (e.resolved_invoice_id=$3 OR EXISTS(SELECT 1 FROM finance_invoice_receipt l WHERE l.tenant_id=e.tenant_id AND l.legal_entity_id=e.legal_entity_id AND l.invoice_id=$3 AND l.receipt_id=e.receipt_id)))",
            claim["tenant_id"],
            claim["legal_entity_id"],
            invoice["id"],
        )
        if hard:
            raise FinanceError("INVOICE_HARD_BLOCK", 409, "关联票据存在未解除硬约束")
        return data

    async def _line_limit(
        self, conn: Connection, claim: Any, invoice: str, line: str, amount: Decimal, tax: Decimal
    ) -> None:
        """指定行仅能使用真实归属的行ID，金额含税累计不得超行。"""
        row = await conn.fetchrow(
            "SELECT data FROM finance_invoice_line WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 AND data->>'id'=$4",
            claim["tenant_id"],
            claim["legal_entity_id"],
            invoice,
            line,
        )
        if row is None:
            raise FinanceError("INVOICE_LINE_INVALID", 422, "指定发票明细不存在或不属于该票")
        data = decode(row["data"])
        gross_limit, tax_limit = await effective_line_limits(conn, claim["tenant_id"], claim["legal_entity_id"], invoice, line, data)
        if amount > gross_limit or tax > tax_limit:
            raise FinanceError("INVOICE_LINE_LIMIT", 409, "累计分配超过发票明细金额或税额")

    async def _reservation_check(
        self, conn: Connection, claim: Any, policy: dict[str, Any], *, lock: bool
    ) -> list[dict[str, Any]]:
        """按票ID顺序锁票并复核当前单据与全部已占额度。"""
        proposals = await conn.fetch(
            "SELECT * FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state='proposed' ORDER BY invoice_id,id",
            claim["tenant_id"],
            claim["legal_entity_id"],
            claim["id"],
        )
        if not proposals and claim["record_mode"] == "internal_record":
            return []
        if sum((row["amount"] for row in proposals), Decimal(0)) != claim["total"]:
            raise FinanceError("ALLOCATION_TOTAL_MISMATCH", 409, "提议分配总额必须严格等于报销总额")
        snapshots = []
        for invoice_id in sorted({row["invoice_id"] for row in proposals}):
            invoice = await conn.fetchrow(
                "SELECT * FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3"
                + (" FOR UPDATE" if lock else ""),
                claim["tenant_id"],
                claim["legal_entity_id"],
                invoice_id,
            )
            for proposal in (row for row in proposals if row["invoice_id"] == invoice_id):
                data = await self._allocation_invoice(
                    conn, claim, invoice, proposal["invoice_version"]
                )
                if policy["require_verified_invoice"] and data["verification_status"] != "passed":
                    raise FinanceError("INVOICE_VERIFICATION_REQUIRED", 409, "发票尚未通过查验")
                snapshots.append(
                    {
                        **allocation_snapshot(proposal),
                        "invoice_version": proposal["invoice_version"],
                        "invoice_line_id": proposal["invoice_line_id"],
                        "invoice": data,
                    }
                )
            await self._invoice_balance_check(conn, claim, invoice)
        return snapshots

    async def _invoice_balance_check(self, conn: Connection, claim: Any, invoice: Any) -> None:
        """汇总全局reserved/consumed和本单proposal，分别保护票额、税额及行。"""
        data = await invoice_projection(conn, claim["tenant_id"], claim["legal_entity_id"], decode(invoice["data"]))
        require_usable_projection(data)
        rows = await conn.fetch(
            "SELECT * FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3 AND (state IN ('reserved','consumed') OR (state='proposed' AND document_id=$4))",
            claim["tenant_id"],
            claim["legal_entity_id"],
            invoice["id"],
            claim["id"],
        )
        amount = sum((row["amount"] for row in rows), Decimal(0))
        tax = sum((row["tax"] for row in rows), Decimal(0))
        if amount > Decimal(data["effective_gross"]) or tax > Decimal(data["effective_tax"]):
            raise FinanceError("INVOICE_BALANCE_EXCEEDED", 409, "累计分配超过可用金额或税额")
        for line in {row["invoice_line_id"] for row in rows if row["invoice_line_id"] is not None}:
            entries = [row for row in rows if row["invoice_line_id"] == line]
            await self._line_limit(
                conn,
                claim,
                invoice["id"],
                line,
                sum((row["amount"] for row in entries), Decimal(0)),
                sum((row["tax"] for row in entries), Decimal(0)),
            )

    async def _reserved_check(
        self, conn: Connection, claim: Any, policy: dict[str, Any], *, lock: bool
    ) -> None:
        """批准前重新检查已预留票证，不让新预检洗白已作废或失败的票。"""
        rows = await conn.fetch(
            "SELECT * FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state IN ('reserved','consumed') ORDER BY invoice_id,id",
            claim["tenant_id"],
            claim["legal_entity_id"],
            claim["id"],
        )
        if not rows and claim["record_mode"] == "internal_record":
            return
        if sum((row["amount"] for row in rows), Decimal(0)) != claim["total"]:
            raise FinanceError("ALLOCATION_TOTAL_MISMATCH", 409, "已预留金额与报销总额不符")
        for id in sorted({row["invoice_id"] for row in rows}):
            invoice = await conn.fetchrow(
                "SELECT * FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3"
                + (" FOR UPDATE" if lock else ""),
                claim["tenant_id"],
                claim["legal_entity_id"],
                id,
            )
            for row in (entry for entry in rows if entry["invoice_id"] == id):
                data = await self._allocation_invoice(conn, claim, invoice, row["invoice_version"])
                if policy["require_verified_invoice"] and data["verification_status"] != "passed":
                    raise FinanceError("INVOICE_VERIFICATION_REQUIRED", 409, "发票当前未通过查验")
            await self._invoice_balance_check(conn, claim, invoice)

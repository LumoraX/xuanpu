"""红票人工关联、税务调整材料与实际待办的独立领域命令。"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f3_projection import invoice_projection
from xuanpu_finance.storage.f3_queries import ComplianceQueryStore

if TYPE_CHECKING:
    from xuanpu_finance.compliance.models import RedLetterInvoiceLink, TaxAdjustmentEvidence
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class ComplianceHumanStore(ComplianceQueryStore):
    """人工只能核实既有事实和补充材料，不能伪造权威结果。"""

    async def link_red_letter_invoice(self, who: FinancePrincipal, id: str, body: RedLetterInvoiceLink, key: str) -> dict[str, Any]:
        """按票ID排序锁蓝红票，人工关联须匹配独立原件、查验、主体和金额。"""
        if who.origin != "human":
            raise FinanceError("HUMAN_REQUIRED", 403, "红票关联必须由实际人工核实")
        async with self.pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow("SELECT * FROM finance_red_letter_case WHERE tenant_id=$1 AND id=$2", who.tenant_id, id)
            if current is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "红字确认单不存在")
            entity = current["legal_entity_id"]
            policy = await self._compliance_policy(conn, who.tenant_id, entity)
            assert policy is not None
            if who.user_id not in policy["tax_executor_user_ids"] or await self._role(conn, who, entity) != "reviewer":
                raise FinanceError("EXECUTOR_FORBIDDEN", 403, "红票关联须由当前税务经办核实")
            request = {"id": id, **body.model_dump(mode="json")}
            previous = await self._business_previous(conn, who, entity, "linkRedLetterInvoice", key, request)
            if previous is not None:
                return previous
            invoices = {}
            for invoice_id in sorted({body.blue_invoice_id, body.red_invoice_id}):
                row = await conn.fetchrow("SELECT * FROM finance_invoice WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE", who.tenant_id, entity, invoice_id)
                if row is None:
                    raise FinanceError("RED_INVOICE_SCOPE_MISMATCH", 409, "蓝红票必须属于相同公司主体")
                invoices[invoice_id] = row
            for invoice_id in sorted(invoices):
                await self._f3_state(conn, invoices[invoice_id], lock=True)
            case = await conn.fetchrow("SELECT * FROM finance_red_letter_case WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE", who.tenant_id, entity, id)
            assert case is not None
            await self._f3_check_red_link(conn, who, case, invoices, body)
            case_data = decode(case["data"])
            await conn.execute("INSERT INTO finance_red_letter_link(tenant_id,legal_entity_id,id,case_id,blue_invoice_id,red_invoice_id,actor_id,amount,tax,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)", who.tenant_id, entity, identifier(), id, body.blue_invoice_id, body.red_invoice_id, who.user_id, Decimal(case_data["amount"]), Decimal(case_data["tax"]), json.dumps(request))
            await conn.execute("UPDATE finance_red_letter_case SET red_invoice_id=$4,version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", who.tenant_id, entity, id, body.red_invoice_id)
            await conn.execute("UPDATE finance_invoice_compliance SET version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3", who.tenant_id, entity, body.blue_invoice_id)
            await self._event(conn, invoices[body.blue_invoice_id], "compliance.red_invoice_linked", {"case_id": id, "red_invoice_id": body.red_invoice_id})
            result = {"id": id, "version": case["version"] + 1, "blue_invoice_id": body.blue_invoice_id, "red_invoice_id": body.red_invoice_id, "status": "linked", "authority_status": case["status"]}
            return await self._business_remember(conn, who, entity, "linkRedLetterInvoice", key, request, result)

    async def _f3_check_red_link(self, conn: Connection, who: FinancePrincipal, case: Any, invoices: dict[str, Any], body: RedLetterInvoiceLink) -> None:
        """已确认单不等于已开票；关联仅承认实际已issued且匹配的红票。"""
        if body.blue_invoice_id == body.red_invoice_id or case["invoice_id"] != body.blue_invoice_id or case["version"] != body.expected_case_version:
            raise FinanceError("RED_INVOICE_SCOPE_MISMATCH", 409, "红票、蓝票和确认单版本不符")
        if case["status"] != "issued" or case["red_invoice_id"] is not None:
            raise FinanceError("RED_INVOICE_NOT_LINKABLE", 409, "仅已实际开具且未关联的权威红票可以关联")
        blue, red = (decode(invoices[id]["data"]) for id in (body.blue_invoice_id, body.red_invoice_id))
        if blue["version"] != body.expected_blue_invoice_version or red["version"] != body.expected_red_invoice_version:
            raise FinanceError("VERSION_CONFLICT", 409, "原蓝票或红票content版本已变化")
        data = decode(case["data"])
        matching = not blue["is_red"] and red["is_red"] and all(blue[field] == red[field] == data[field] for field in ("buyer_tax_id", "seller_tax_id"))
        matching = matching and abs(Decimal(red["gross"])) == Decimal(data["amount"]) and abs(Decimal(red["tax"])) == Decimal(data["tax"])
        matching = matching and red["number"] == data["red_invoice_number"] and invoices[body.blue_invoice_id]["source_simulation"] == invoices[body.red_invoice_id]["source_simulation"]
        if not matching:
            raise FinanceError("RED_INVOICE_FACT_MISMATCH", 409, "红票主体、实际号码、金额税额或来源与权威确认单不符")
        for invoice_id in (body.blue_invoice_id, body.red_invoice_id):
            projection = await invoice_projection(conn, who.tenant_id, case["legal_entity_id"], decode(invoices[invoice_id]["data"]))
            originals = await conn.fetchval("SELECT count(*) FROM finance_invoice_receipt WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3", who.tenant_id, case["legal_entity_id"], invoice_id)
            if projection["verification_status"] != "passed" or not originals:
                raise FinanceError("VALID_VERIFICATION_REQUIRED", 409, "蓝红票必须独立导入原件并取得当前有效查验")
        duplicate = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM finance_red_letter_link WHERE tenant_id=$1 AND legal_entity_id=$2 AND red_invoice_id=$3)", who.tenant_id, case["legal_entity_id"], body.red_invoice_id)
        if duplicate:
            raise FinanceError("RED_INVOICE_ALREADY_LINKED", 409, "同一红票不能重复关联")

    async def record_tax_adjustment_evidence(self, who: FinancePrincipal, id: str, body: TaxAdjustmentEvidence, key: str) -> dict[str, Any]:
        """材料收到独立追加，不把补件声明变成申报完成。"""
        if who.origin != "human":
            raise FinanceError("HUMAN_REQUIRED", 403, "税务调整补件须由实际人工明确提交")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM finance_tax_adjustment WHERE tenant_id=$1 AND id=$2", who.tenant_id, id)
            if row is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "税务调整义务不存在")
            target = await self._f3_target(conn, who, "invoice", row["invoice_id"], lock=True)
            if target["simulation"] != await self._simulation(conn):
                raise FinanceError("SIMULATION_MODE_CONFLICT", 409, "调整义务原票来源与当前财务环境不一致")
            entity = row["legal_entity_id"]
            policy = await self._compliance_policy(conn, who.tenant_id, entity)
            assert policy is not None
            if who.user_id not in policy["tax_executor_user_ids"] or await self._role(conn, who, entity) != "reviewer":
                raise FinanceError("EXECUTOR_FORBIDDEN", 403, "仅当前税务经办可以补充材料")
            evidence = await conn.fetch("SELECT r.id,o.source_simulation FROM finance_receipt r JOIN finance_operation o ON(o.tenant_id=r.tenant_id AND o.legal_entity_id=r.legal_entity_id AND o.id=r.operation_id) WHERE r.tenant_id=$1 AND r.legal_entity_id=$2 AND r.id=ANY($3::text[]) AND r.sha256<>''", who.tenant_id, entity, body.evidence_ids)
            if {r["id"] for r in evidence} != set(body.evidence_ids):
                raise FinanceError("EVIDENCE_FORBIDDEN", 403, "补件材料必须来自相同公司已收到的真实原件")
            if any(r["source_simulation"] != target["simulation"] for r in evidence):
                raise FinanceError("EVIDENCE_SIMULATION_CONFLICT", 403, "补件材料原始接收环境与调整义务原票来源不一致")
            request = {"id": id, **body.model_dump(mode="json")}
            previous = await self._business_previous(conn, who, entity, "recordTaxAdjustmentEvidence", key, request)
            if previous is not None:
                return previous
            row = await conn.fetchrow("SELECT * FROM finance_tax_adjustment WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 FOR UPDATE", who.tenant_id, entity, id)
            assert row is not None
            if row["version"] != body.expected_version:
                raise FinanceError("VERSION_CONFLICT", 409, "税务调整义务已变化")
            await conn.execute("INSERT INTO finance_tax_adjustment_evidence(tenant_id,legal_entity_id,id,adjustment_id,actor_id,data) VALUES($1,$2,$3,$4,$5,$6::jsonb)", who.tenant_id, entity, identifier(), id, who.user_id, json.dumps(request))
            await conn.execute("UPDATE finance_tax_adjustment SET version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", who.tenant_id, entity, id)
            await conn.execute("UPDATE finance_invoice_compliance SET version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3", who.tenant_id, entity, row["invoice_id"])
            await self._event(conn, {"tenant_id": who.tenant_id, "legal_entity_id": entity, "id": row["invoice_id"]}, "compliance.adjustment_material_received", {"adjustment_id": id, "evidence_ids": body.evidence_ids})
            result = {"id": id, "version": row["version"] + 1, "status": row["status"], "material_status": "received", "evidence_ids": body.evidence_ids, "declaration_status": "not_checked"}
            return await self._business_remember(conn, who, entity, "recordTaxAdjustmentEvidence", key, request, result)

"""F0/F1/F2与F3实际人工待办的统一可恢复读取。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.expenses.models import ActionParameters, ActionPreview
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, encode_cursor, page, timestamp_cursor
from xuanpu_finance.storage.f2_plans import EXTERNAL_ACTIONS
from xuanpu_finance.storage.f3_human import ComplianceHumanStore

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class ComplianceWorkflowQueryStore(ComplianceHumanStore):
    """统一入口只合并独立事实，不把合规计划放进费用单据表。"""

    async def list_review_queue(self, who: FinancePrincipal, entity: str, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """全部当前人工待办共用独立全量计数和稳定游标。"""
        scope = request_digest([who.tenant_id, who.user_id, entity, "review_queue"])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            if await self._role(conn, who, entity) not in {"reviewer", "approver"}:
                return page([], 0, None)
            old = await conn.fetch("SELECT c.*,s.id AS step_id,s.version AS step_version,s.approver_user_id,w.id AS workflow_id,w.version AS workflow_version,w.due_at,w.target_type,w.target_id AS workflow_target_id FROM finance_workflow_step s JOIN finance_workflow w ON(w.tenant_id=s.tenant_id AND w.legal_entity_id=s.legal_entity_id AND w.id=s.workflow_id) JOIN finance_expense_claim c ON(c.tenant_id=w.tenant_id AND c.legal_entity_id=w.legal_entity_id AND c.id=w.document_id) WHERE s.tenant_id=$1 AND s.legal_entity_id=$2 AND s.approver_user_id=$3 AND s.status='pending' AND w.status='under_review'", who.tenant_id, entity, who.user_id)
            new = await conn.fetch("SELECT p.*,s.id AS step_id,s.version AS step_version,s.approver_user_id,w.id AS workflow_id,w.version AS workflow_version,p.expires_at AS due_at FROM finance_compliance_workflow_step s JOIN finance_compliance_workflow w ON(w.tenant_id=s.tenant_id AND w.legal_entity_id=s.legal_entity_id AND w.id=s.workflow_id) JOIN finance_compliance_plan p ON(p.tenant_id=w.tenant_id AND p.legal_entity_id=w.legal_entity_id AND p.id=w.plan_id) JOIN finance_grant_epoch e ON(e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id AND e.user_id=s.approver_user_id AND e.generation=s.authority_generation AND e.role IN ('reviewer','approver')) WHERE s.tenant_id=$1 AND s.legal_entity_id=$2 AND s.approver_user_id=$3 AND s.status='pending' AND w.status='under_review'", who.tenant_id, entity, who.user_id)
            all_rows = sorted([(r, False) for r in old] + [(r, True) for r in new], key=lambda pair: (pair[0]["created_at"], pair[0]["step_id"]))
            selected = [(r, f3) for r, f3 in all_rows if stamp is None or (r["created_at"], r["step_id"]) > (stamp, last or "")][:limit + 1]
            items = [await self._f3_review_item(conn, who, r) if f3 else await self._legacy_review_item(conn, who, r) for r, f3 in selected[:limit]]
            return page(items, len(all_rows), encode_cursor(scope, [selected[limit - 1][0]["created_at"].isoformat(), selected[limit - 1][0]["step_id"]]) if len(selected) > limit else None)

    async def _legacy_review_item(self, conn: Connection, who: FinancePrincipal, row: Any) -> dict[str, Any]:
        """保留旧业务和外部行动待办原语义。"""
        if row["target_type"] != "action_plan":
            return await self._review_item(conn, who, row)
        original = await self._plan_row(conn, who, row["workflow_target_id"])
        payload = decode(original["data"])
        body = ActionPreview(action="approve", target_type="action_plan", target_id=original["id"], expected_version=1, parameters=ActionParameters(workflow_step_id=row["step_id"]))
        blockers = await self._check_plan_review(conn, who, original, body)
        return {"workflow_step_id": row["step_id"], "target_id": original["id"], "target_type": "action_plan", "target_version": 1, "summary": payload["action"] + " " + payload["parameters"]["amount"] + " 元", "total": payload["parameters"]["amount"], "evidence": await self._claim_evidence(conn, row), "workflow_id": row["workflow_id"], "workflow_version": row["workflow_version"], "step_version": row["step_version"], "approver_user_id": row["approver_user_id"], "status": "pending", "due_at": row["due_at"].isoformat(), "blockers": blockers}

    async def _f3_review_item(self, conn: Connection, who: FinancePrincipal, row: Any) -> dict[str, Any]:
        """合规待办明确目标是独立行动计划而非不存在的费用父单据。"""
        payload = decode(row["data"])
        from xuanpu_finance.errors import FinanceError

        blockers = []
        try:
            await self._f3_check_bindings(conn, row)
        except FinanceError as exc:
            blockers.append(exc.code + ":" + exc.message)
        return {"workflow_step_id": row["step_id"], "target_id": row["id"], "target_type": "action_plan", "target_version": 1, "summary": payload["action"], "total": payload["bindings"].get("amount", self._f3_amount(payload["bindings"]["target"])), "evidence": await self._f3_review_evidence_refs(conn, row), "workflow_id": row["workflow_id"], "workflow_version": row["workflow_version"], "step_version": row["step_version"], "approver_user_id": row["approver_user_id"], "status": "pending", "due_at": row["expires_at"].isoformat(), "blockers": blockers}

    async def _f3_review_evidence_refs(self, conn: Connection, plan: Any) -> list[dict[str, Any]]:
        """待办展示固定目标的实际原件和已保存渠道回执引用。"""
        target = decode(plan["data"])["bindings"]["target"]
        invoices = [target["id"]] if target["type"] == "invoice" else [target["parent"]["id"]] if target["type"] == "red_letter_case" else [i["id"] for i in target.get("invoices", [])]
        rows = await conn.fetch("SELECT DISTINCT r.id,r.sha256,r.file_ref FROM finance_invoice_receipt l JOIN finance_receipt r ON(r.tenant_id=l.tenant_id AND r.legal_entity_id=l.legal_entity_id AND r.id=l.receipt_id) WHERE l.tenant_id=$1 AND l.legal_entity_id=$2 AND l.invoice_id=ANY($3::text[]) ORDER BY r.id", plan["tenant_id"], plan["legal_entity_id"], invoices)
        evidence = [{"id": r["id"], "kind": "source", "sha256": r["sha256"], "source_version": str(decode(r["file_ref"])["revision"]), "verification_state": "not_checked"} for r in rows]
        receipts = await conn.fetch("SELECT r.id,r.sha256 FROM finance_compliance_receipt r JOIN finance_compliance_operation o ON(o.tenant_id=r.tenant_id AND o.legal_entity_id=r.legal_entity_id AND o.id=r.operation_id) WHERE o.tenant_id=$1 AND o.legal_entity_id=$2 AND o.target_type='invoice' AND o.target_id=ANY($3::text[]) ORDER BY r.created_at,r.id", plan["tenant_id"], plan["legal_entity_id"], invoices)
        evidence.extend({"id": r["id"], "kind": "provider_receipt", "sha256": r["sha256"], "verification_state": "not_checked"} for r in receipts)
        return evidence

    async def list_action_plans(self, who: FinancePrincipal, entity: str, target_id: str | None = None, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """原行动恢复入口同时支持F2和独立F3计划的权限过滤。"""
        scope = request_digest([who.tenant_id, who.user_id, entity, "plans", target_id])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            role = await self._role(conn, who, entity)
            old = await conn.fetch("SELECT p.* FROM finance_action_plan p WHERE p.tenant_id=$1 AND p.legal_entity_id=$2 AND p.data->>'action'=ANY($3::text[]) AND ($4::text IS NULL OR p.target_id=$4 OR p.data->>'target_id'=$4) AND (p.owner_id=$5 OR $6::text='reviewer' OR ($6::text='approver' AND EXISTS(SELECT 1 FROM finance_workflow w JOIN finance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) WHERE w.tenant_id=p.tenant_id AND w.legal_entity_id=p.legal_entity_id AND w.target_type='action_plan' AND w.target_id=p.id AND s.approver_user_id=$5)))", who.tenant_id, entity, list(EXTERNAL_ACTIONS), target_id, who.user_id, role)
            new = await conn.fetch("SELECT * FROM finance_compliance_plan WHERE tenant_id=$1 AND legal_entity_id=$2 AND target_type<>'action_plan' AND ($3::text IS NULL OR target_id=$3)", who.tenant_id, entity, target_id)
            visible = [r for r in new if await self._f3_visible(conn, who, r, "plans")]
            all_rows = sorted([(r, False) for r in old] + [(r, True) for r in visible], key=lambda pair: (pair[0]["expires_at"], pair[0]["id"]))
            selected = [(r, f3) for r, f3 in all_rows if stamp is None or (r["expires_at"], r["id"]) > (stamp, last or "")][:limit + 1]
            items = [await self._f3_plan_view(conn, who, r) if f3 else await self._plan_view(conn, who, r) for r, f3 in selected[:limit]]
            return page(items, len(all_rows), encode_cursor(scope, [selected[limit - 1][0]["expires_at"].isoformat(), selected[limit - 1][0]["id"]]) if len(selected) > limit else None)

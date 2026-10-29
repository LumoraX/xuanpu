"""F3不可变行动计划、专用实际审批及实时授权复核。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from xuanpu_finance.compliance.models import ComplianceActionParameters, ComplianceActionPreview
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f3_archive_inputs import ComplianceArchiveInputStore

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ActionPreview
    from xuanpu_finance.storage.business_base import Connection


class CompliancePlanStore(ComplianceArchiveInputStore):
    """计划、审批与执行分别持久化，不以预检生成批准事实。"""

    async def preview_finance_action(self, who: FinancePrincipal, body: ActionPreview | ComplianceActionPreview, key: str) -> dict[str, Any]:
        """F3目标与前缀单独派发，旧F0/F1/F2继续原行为。"""
        if not isinstance(body, ComplianceActionPreview):
            if body.target_type == "action_plan" and body.target_id.startswith("f3plan_"):
                body = ComplianceActionPreview.model_validate(body.model_dump(mode="json", exclude_none=True))
            else:
                return await super().preview_finance_action(who, body, key)
        if body.target_type == "action_plan":
            return await self._f3_preview_review(who, body, key)
        async with self.pool.acquire() as conn, conn.transaction():
            target = await self._f3_target(conn, who, body.target_type, body.target_id, lock=True)
            entity = target["legal_entity_id"]
            request = body.model_dump(mode="json", exclude_none=True)
            old = await self._business_previous(conn, who, entity, "previewComplianceAction", key, request)
            if old is not None:
                return await self._f3_plan_view(conn, who, await self._f3_plan_row(conn, who, old["id"]))
            blockers: list[str] = []
            bindings: dict[str, Any] = {}
            steps: list[dict[str, Any]] = []
            try:
                bindings, steps = await self._f3_bindings(conn, who, target, body)
                if body.action == "archive":
                    bindings["archive_snapshot"] = await self._f3_archive_snapshot(conn, target, body, bindings["policy"])
            except FinanceError as exc:
                blockers.append(exc.code + ":" + exc.message)
            expires = datetime.now(UTC) + timedelta(hours=bindings.get("policy", {}).get("plan_hours", 24))
            if "confirmation_deadline" in bindings:
                expires = min(expires, datetime.fromisoformat(bindings["confirmation_deadline"]))
            payload = {"id": "f3plan_" + identifier(), "version": 1, "tenant_id": who.tenant_id, "legal_entity_id": entity, "actor_id": who.user_id, "owner_id": target["owner_id"], "action": body.action, "target_type": body.target_type, "target_id": body.target_id, "target_version": target["version"], "parameters": request["parameters"], "simulation": target["simulation"], "expires_at": expires.isoformat(), "bindings": bindings, "required_approvals": [s["approver_user_id"] for s in steps], "effects": ["按固定原始事实执行合规动作，受理不代表权威成功"], "initial_blockers": blockers}
            plan = await self._f3_insert_plan(conn, payload)
            if not blockers:
                await self._f3_create_workflow(conn, plan, steps)
                blockers.append("ACTION_APPROVAL_REQUIRED:固定合规行动仍需专用人工批准")
            return await self._business_remember(conn, who, entity, "previewComplianceAction", key, request, self._f3_public_plan(plan, blockers))

    async def _f3_insert_plan(self, conn: Connection, payload: dict[str, Any]) -> Any:
        """独立目标计划只外键公司，不强造费用单据。"""
        digest = request_digest(payload)
        return await conn.fetchrow("INSERT INTO finance_compliance_plan(tenant_id,legal_entity_id,id,actor_id,owner_id,target_type,target_id,target_version,digest,expires_at,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb) RETURNING *", payload["tenant_id"], payload["legal_entity_id"], payload["id"], payload["actor_id"], payload["owner_id"], payload["target_type"], payload["target_id"], payload["target_version"], digest, datetime.fromisoformat(payload["expires_at"]), json.dumps(payload))

    async def _f3_create_workflow(self, conn: Connection, plan: Any, steps: list[dict[str, Any]]) -> None:
        """实际审批指派绑定当前岗位代数及原计划。"""
        workflow_id = "f3wf_" + identifier()
        await conn.execute("INSERT INTO finance_compliance_workflow(tenant_id,legal_entity_id,id,plan_id) VALUES($1,$2,$3,$4)", plan["tenant_id"], plan["legal_entity_id"], workflow_id, plan["id"])
        for position, step in enumerate(steps):
            generation = await conn.fetchval("SELECT generation FROM finance_grant_epoch WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 AND role IN ('reviewer','approver')", plan["tenant_id"], plan["legal_entity_id"], step["approver_user_id"])
            if generation is None:
                raise FinanceError("APPROVER_FORBIDDEN", 409, "实际审批岗位已失效")
            await conn.execute("INSERT INTO finance_compliance_workflow_step(tenant_id,legal_entity_id,id,workflow_id,position,approver_user_id,authority_generation,status) VALUES($1,$2,$3,$4,$5,$6,$7,$8)", plan["tenant_id"], plan["legal_entity_id"], "f3step_" + identifier(), workflow_id, position, step["approver_user_id"], generation, "pending" if position == 0 else "waiting")

    async def _f3_plan_row(self, conn: Connection, who: FinancePrincipal, id: str) -> Any:
        """计划仅供原相关人员、当前reviewer及有效实际指派读取。"""
        row = await conn.fetchrow("SELECT * FROM finance_compliance_plan WHERE tenant_id=$1 AND id=$2", who.tenant_id, id)
        if row is None:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "合规行动计划不存在")
        role = await self._role(conn, who, row["legal_entity_id"])
        assigned = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM finance_compliance_workflow w JOIN finance_compliance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) JOIN finance_grant_epoch e ON(e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id AND e.user_id=s.approver_user_id AND e.generation=s.authority_generation AND e.role IN ('reviewer','approver')) WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.plan_id=$3 AND s.approver_user_id=$4)", who.tenant_id, row["legal_entity_id"], id, who.user_id)
        if role != "reviewer" and who.user_id not in {row["actor_id"], row["owner_id"]} and not assigned:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "合规计划不存在")
        return row

    def _f3_public_plan(self, plan: Any, blockers: list[str]) -> dict[str, Any]:
        """只公开实际可见的固定计划和动态阻断，不泄露内部权限快照。"""
        payload = decode(plan["data"])
        fields = ("id", "version", "legal_entity_id", "action", "target_type", "target_id", "target_version", "parameters", "simulation", "expires_at", "required_approvals", "effects")
        result = {**{k: payload[k] for k in fields}, "digest": plan["digest"], "prepared_by": payload["actor_id"], "owner_id": payload["owner_id"], "executable": not blockers, "blockers": blockers}
        bindings = payload["bindings"]
        if "target" in bindings:
            result["amount"] = bindings.get("amount", self._f3_amount(bindings["target"]))
        if "tax" in bindings:
            result["tax"] = bindings["tax"]
        return result

    async def _f3_plan_view(self, conn: Connection, who: FinancePrincipal, plan: Any) -> dict[str, Any]:
        """读取时仍复核全部当前绑定，历史批准不等于当前可执行。"""
        payload = decode(plan["data"])
        blockers: list[str] = []
        try:
            if payload["target_type"] == "action_plan":
                await self._f3_review_checks(conn, who, plan)
            elif payload["action"] not in {"verify_invoice", "refresh_red_letter", "audit_archive"}:
                await self._f3_check_bindings(conn, plan)
                grant = await conn.fetchval("SELECT active FROM finance_compliance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3 AND plan_digest=$4", plan["tenant_id"], plan["legal_entity_id"], plan["id"], plan["digest"])
                if grant is not True:
                    raise FinanceError("ACTION_APPROVAL_REQUIRED", 409, "固定合规行动仍需专用人工批准")
        except FinanceError as exc:
            blockers.append(exc.code + ":" + exc.message)
        result = self._f3_public_plan(plan, blockers)
        workflow = await conn.fetchrow("SELECT * FROM finance_compliance_workflow WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3", plan["tenant_id"], plan["legal_entity_id"], plan["id"])
        if workflow is not None:
            steps = await conn.fetch("SELECT * FROM finance_compliance_workflow_step WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 ORDER BY position", plan["tenant_id"], plan["legal_entity_id"], workflow["id"])
            policy = payload["bindings"].get("policy", {})
            step_config = policy.get("archive_steps" if payload["action"] in {"archive", "audit_archive"} else "tax_steps", [])
            result["workflow"] = {"id": workflow["id"], "version": workflow["version"], "status": workflow["status"], "current_step_id": next((s["id"] for s in steps if s["status"] == "pending"), None), "target_version": 1, "policy_version": policy.get("version", 1), "provider": "internal", "due_at": payload["expires_at"], "steps": [{**{k: s[k] for k in ("id", "version", "approver_user_id", "status")}, "name": step_config[i]["name"] if i < len(step_config) else "合规审核"} for i, s in enumerate(steps)]}
        return result

    async def _f3_check_bindings(self, conn: Connection, plan: Any, *, lock: bool = False) -> dict[str, Any]:
        """版本、岗位、渠道和政策的任一代数变化均永久使旧计划失效。"""
        payload = decode(plan["data"])
        if plan["expires_at"] <= datetime.now(UTC):
            raise FinanceError("PLAN_EXPIRED", 409, "合规行动计划已过期")
        if payload["initial_blockers"]:
            code, _, message = payload["initial_blockers"][0].partition(":")
            raise FinanceError(code, 409, message)
        who = FinancePrincipal(plan["tenant_id"], plan["actor_id"], "human")
        body = ComplianceActionPreview(action=payload["action"], target_type=payload["target_type"], target_id=payload["target_id"], expected_version=payload["target_version"], parameters=ComplianceActionParameters.model_validate(payload["parameters"]))
        target = await self._f3_target(conn, who, body.target_type, body.target_id, lock=lock)
        bindings, _ = await self._f3_bindings(conn, who, target, body)
        if body.action == "archive":
            bindings["archive_snapshot"] = await self._f3_archive_snapshot(conn, target, body, bindings["policy"], anchor=payload["bindings"]["archive_snapshot"]["retention"]["anchor_at"])
        if request_digest(bindings) != request_digest(payload["bindings"]):
            raise FinanceError("PLAN_BINDINGS_CHANGED", 409, "目标、政策、岗位、渠道或材料版本已经变化")
        return target

    async def _f3_preview_review(self, who: FinancePrincipal, body: ComplianceActionPreview, key: str) -> dict[str, Any]:
        """批准决定也冻结为原人工的独立计划，但不循环要求再次审批。"""
        async with self.pool.acquire() as conn, conn.transaction():
            original = await self._f3_plan_row(conn, who, body.target_id)
            request = body.model_dump(mode="json", exclude_none=True)
            old = await self._business_previous(conn, who, original["legal_entity_id"], "previewComplianceReview", key, request)
            if old is not None:
                return await self._f3_plan_view(conn, who, await self._f3_plan_row(conn, who, old["id"]))
            payload = {"id": "f3plan_" + identifier(), "version": 1, "tenant_id": who.tenant_id, "legal_entity_id": original["legal_entity_id"], "actor_id": who.user_id, "owner_id": original["owner_id"], "action": body.action, "target_type": "action_plan", "target_id": original["id"], "target_version": body.expected_version, "parameters": request["parameters"], "simulation": decode(original["data"])["simulation"], "expires_at": original["expires_at"].isoformat(), "bindings": {"original_digest": original["digest"]}, "required_approvals": [], "effects": ["追加当前实际审批人的明确人工决定"], "initial_blockers": []}
            plan = await self._f3_insert_plan(conn, payload)
            result = await self._f3_plan_view(conn, who, plan)
            return await self._business_remember(conn, who, original["legal_entity_id"], "previewComplianceReview", key, request, result)

    async def _f3_review_checks(self, conn: Connection, who: FinancePrincipal, plan: Any) -> tuple[Any, Any, Any]:
        """原owner/preparer、过期指派及错误步骤永远不能形成审批授权。"""
        payload = decode(plan["data"])
        original = await self._f3_plan_row(conn, who, payload["target_id"])
        if payload["target_version"] != 1 or payload["bindings"]["original_digest"] != original["digest"]:
            raise FinanceError("PLAN_DIGEST_MISMATCH", 409, "审批引用原计划不符")
        await self._f3_check_bindings(conn, original)
        if who.user_id in {original["owner_id"], original["actor_id"]}:
            raise FinanceError("SELF_APPROVAL_FORBIDDEN", 403, "申请人和经办本人不得审批")
        workflow = await conn.fetchrow("SELECT * FROM finance_compliance_workflow WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3 FOR UPDATE", who.tenant_id, original["legal_entity_id"], original["id"])
        if workflow is None or workflow["status"] != "under_review":
            raise FinanceError("WORKFLOW_NOT_PENDING", 409, "合规审批已结束或不存在")
        step = await conn.fetchrow("SELECT * FROM finance_compliance_workflow_step WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 AND status='pending' FOR UPDATE", who.tenant_id, original["legal_entity_id"], workflow["id"])
        if step is None or step["id"] != payload["parameters"]["workflow_step_id"] or step["approver_user_id"] != who.user_id:
            raise FinanceError("WORKFLOW_STEP_FORBIDDEN", 403, "仅当前实际分派审批人可决定")
        generation = await conn.fetchval("SELECT generation FROM finance_grant_epoch WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 AND role IN ('reviewer','approver') FOR SHARE", who.tenant_id, original["legal_entity_id"], who.user_id)
        if generation != step["authority_generation"]:
            raise FinanceError("AUTHORITY_CHANGED", 409, "实际审批岗位已撤销或发生代数变化")
        await self._f3_review_evidence(conn, original, payload["parameters"].get("evidence_ids", []))
        return original, workflow, step

    async def _f3_review_evidence(self, conn: Connection, original: Any, evidence_ids: list[str]) -> None:
        """批准所附材料也只能来自固定目标的原件或当前单据证据。"""
        if not evidence_ids:
            return
        payload = decode(original["data"])
        target = payload["bindings"]["target"]
        invoice_ids = [target["id"]] if target["type"] == "invoice" else [target["parent"]["id"]] if target["type"] == "red_letter_case" else [i["id"] for i in target.get("invoices", [])]
        rows = await conn.fetch("SELECT DISTINCT r.id FROM finance_receipt r WHERE r.tenant_id=$1 AND r.legal_entity_id=$2 AND r.id=ANY($3::text[]) AND (EXISTS(SELECT 1 FROM finance_invoice_receipt l WHERE l.tenant_id=r.tenant_id AND l.legal_entity_id=r.legal_entity_id AND l.receipt_id=r.id AND l.invoice_id=ANY($4::text[])) OR EXISTS(SELECT 1 FROM finance_expense_evidence e WHERE e.tenant_id=r.tenant_id AND e.legal_entity_id=r.legal_entity_id AND e.receipt_id=r.id AND e.document_id=$5))", original["tenant_id"], original["legal_entity_id"], evidence_ids, invoice_ids, target["id"])
        if {r["id"] for r in rows} != set(evidence_ids):
            raise FinanceError("EVIDENCE_FORBIDDEN", 403, "审批材料不属于固定合规目标实际原件")

    async def get_action_plan(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """统一读取按前缀派发独立合规计划。"""
        if not id.startswith("f3plan_"):
            return await super().get_action_plan(who, id)
        async with self.pool.acquire() as conn, conn.transaction():
            return await self._f3_plan_view(conn, who, await self._f3_plan_row(conn, who, id))

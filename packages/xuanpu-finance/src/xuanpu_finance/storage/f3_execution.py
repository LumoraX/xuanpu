"""F3实际人工决定、独立任务受理与确定性请求冻结。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f3_plans import CompliancePlanStore
from xuanpu_finance.storage.f3_reservations import assert_compliance_dimension

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ActionExecution
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class ComplianceExecutionStore(CompliancePlanStore):
    """所有副作用先持久受理；实际网络动作仍在事务之外。"""

    async def execute_finance_action(self, who: FinancePrincipal, body: ActionExecution, key: str) -> dict[str, Any]:
        """合规执行必须是原经办真人，实际审批人不能借计划提升身份。"""
        if not body.plan_id.startswith("f3plan_"):
            return await super().execute_finance_action(who, body, key)
        if who.origin != "human":
            raise FinanceError("HUMAN_REQUIRED", 403, "合规执行必须由实际人工明确决定")
        async with self.pool.acquire() as conn, conn.transaction():
            plan = await self._f3_plan_row(conn, who, body.plan_id)
            payload = decode(plan["data"])
            if plan["actor_id"] != who.user_id:
                raise FinanceError("PLAN_OWNER_REQUIRED", 403, "只有原合规经办人可执行固定计划")
            if plan["digest"] != body.plan_digest:
                raise FinanceError("PLAN_DIGEST_MISMATCH", 409, "原合规计划摘要不符")
            if payload["target_type"] == "action_plan":
                original = await self._f3_plan_row(conn, who, payload["target_id"])
                original_data = decode(original["data"])
                await self._f3_target(conn, who, original_data["target_type"], original_data["target_id"], lock=True)
            else:
                await self._f3_target(conn, who, payload["target_type"], payload["target_id"], lock=True)
            request = body.model_dump(mode="json")
            old = await self._business_previous(conn, who, plan["legal_entity_id"], "executeComplianceAction", key, request)
            if old is not None:
                return old
            existing = await conn.fetchrow("SELECT * FROM finance_compliance_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3", who.tenant_id, plan["legal_entity_id"], plan["id"])
            if existing is not None:
                result = self._f3_operation_view(existing)
            elif payload["target_type"] == "action_plan":
                await self._f3_apply_review(conn, who, plan)
                result = await self._f3_accept_operation(conn, plan, local=True)
            else:
                await self._f3_check_bindings(conn, plan)
                grant = await conn.fetchval("SELECT active FROM finance_compliance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3 AND plan_digest=$4 FOR UPDATE", who.tenant_id, plan["legal_entity_id"], plan["id"], plan["digest"])
                if grant is not True:
                    raise FinanceError("ACTION_APPROVAL_REQUIRED", 409, "尚未取得当前固定合规计划的实际人工批准")
                result = await self._f3_accept_operation(conn, plan)
            return await self._business_remember(conn, who, plan["legal_entity_id"], "executeComplianceAction", key, request, result)

    async def _f3_apply_review(self, conn: Connection, who: FinancePrincipal, plan: Any) -> None:
        """每一步实际决定不可变，最后一步才授予原摘要执行权。"""
        original, workflow, step = await self._f3_review_checks(conn, who, plan)
        payload = decode(plan["data"])
        approved = payload["action"] == "approve"
        scope = (who.tenant_id, plan["legal_entity_id"])
        await conn.execute("INSERT INTO finance_compliance_decision(tenant_id,legal_entity_id,id,workflow_id,step_id,actor_id,action,plan_digest,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)", *scope, identifier(), workflow["id"], step["id"], who.user_id, payload["action"], original["digest"], json.dumps(payload["parameters"]))
        await conn.execute("UPDATE finance_compliance_workflow_step SET status=$4,version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", *scope, step["id"], "approved" if approved else "rejected")
        waiting = await conn.fetchrow("SELECT * FROM finance_compliance_workflow_step WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 AND status='waiting' ORDER BY position LIMIT 1", *scope, workflow["id"])
        status = "under_review" if approved and waiting is not None else "approved" if approved else "rejected"
        if approved and waiting is not None:
            await conn.execute("UPDATE finance_compliance_workflow_step SET status='pending',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", *scope, waiting["id"])
        if not approved:
            await conn.execute("UPDATE finance_compliance_workflow_step SET status='cancelled',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 AND status='waiting'", *scope, workflow["id"])
        await conn.execute("UPDATE finance_compliance_workflow SET status=$4,version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", *scope, workflow["id"], status)
        if status == "approved":
            await conn.execute("INSERT INTO finance_compliance_grant(tenant_id,legal_entity_id,plan_id,plan_digest) VALUES($1,$2,$3,$4)", *scope, original["id"], original["digest"])

    async def _f3_accept_operation(self, conn: Connection, plan: Any, *, local: bool = False) -> dict[str, Any]:
        """固定请求在受理时即形成模板，prepare只复核后转为不可重发事实。"""
        payload = decode(plan["data"])
        if not local:
            await assert_compliance_dimension(conn, plan)
        provider = payload["bindings"].get("provider", {})
        op_id, request_id = "f3op_" + identifier(), "f3req_" + identifier()
        request = self._f3_request(plan, op_id, request_id) if not local else {}
        row = await conn.fetchrow("INSERT INTO finance_compliance_operation(tenant_id,legal_entity_id,id,owner_id,target_type,target_id,plan_id,action,simulation,provider,configuration_version,adapter_version,external_request_id,request_template,status,business_result) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb,$15,$16) RETURNING *", plan["tenant_id"], plan["legal_entity_id"], op_id, plan["actor_id"], payload["target_type"], payload["target_id"], plan["id"], payload["action"], payload["simulation"], provider.get("provider", "local"), provider.get("configuration_version", 1), provider.get("adapter_version", "local-v1"), request_id, json.dumps(request), "succeeded" if local else "accepted", "completed" if local else "pending")
        if not local and payload["action"] not in {"archive", "audit_archive"}:
            target_id = payload["target_id"]
            if payload["target_type"] == "red_letter_case":
                target_id = payload["bindings"]["target"]["parent"]["id"]
            await self._event(conn, {"tenant_id": plan["tenant_id"], "legal_entity_id": plan["legal_entity_id"], "id": target_id}, "compliance.action_accepted", {"operation_id": op_id, "plan_id": plan["id"], "action": payload["action"]})
        return self._f3_operation_view(row)

    def _f3_request(self, plan: Any, operation_id: str, external_request_id: str) -> dict[str, Any]:
        """请求摘要覆盖除自身外全部固定信封和业务payload。"""
        payload = decode(plan["data"])
        bindings = payload["bindings"]
        provider = bindings["provider"]
        target = bindings["target"]
        business = {"target_snapshot": target, "parameters": payload["parameters"]}
        for name in ("amount", "tax", "accounting_period", "usage_kind", "archive_snapshot", "archive_request"):
            if name in bindings:
                business[name] = bindings[name]
        request = {"protocol_version": "1", "tenant_id": plan["tenant_id"], "legal_entity_id": plan["legal_entity_id"], "operation_id": operation_id, "action": payload["action"], "external_request_id": external_request_id, "provider": provider["provider"], "adapter_version": provider["adapter_version"], "configuration_version": provider["configuration_version"], "simulation": payload["simulation"], "amount": bindings.get("amount", self._f3_amount(target)), "currency": "CNY", "target": {"id": target["id"], "type": target["type"], "version": target["version"]}, "plan_digest": plan["digest"], "payload": business}
        request["payload_hash"] = request_digest(request)
        return request

    def _f3_operation_view(self, row: Any) -> dict[str, Any]:
        """仅暴露持久事实和固定请求引用，受理不称成功。"""
        return {"id": row["id"], "version": row["version"], "legal_entity_id": row["legal_entity_id"], "action": row["action"], "target_type": row["target_type"], "target_id": row["target_id"], "plan_id": row["plan_id"], "status": row["status"], "business_result": row["business_result"], "simulation": row["simulation"], "external_request_id": row["external_request_id"], "last_error": row["last_error"]}

    async def verify_invoice(self, who: FinancePrincipal, id: str, expected_version: int, key: str) -> dict[str, Any]:
        """查验可由有权agent受理，只创建只读外查任务。"""
        return await self._f3_read_action(who, "invoice", id, expected_version, key, "verify_invoice")

    async def refresh_red_letter_case(self, who: FinancePrincipal, id: str, expected_version: int, key: str) -> dict[str, Any]:
        """已有确认单刷新只查询，不建单、不开票。"""
        return await self._f3_read_action(who, "red_letter_case", id, expected_version, key, "refresh_red_letter")

    async def audit_archive(self, who: FinancePrincipal, id: str, expected_version: int, key: str) -> dict[str, Any]:
        """档案审计只回读原包，不上传新档案且不要求副作用审批。"""
        return await self._f3_read_action(who, "archive", id, expected_version, key, "audit_archive")

    async def _f3_read_action(self, who: FinancePrincipal, kind: str, id: str, version: int, key: str, action: str) -> dict[str, Any]:
        """读取类任务同样冻结租户、主体、来源和幂等范围。"""
        request = {"kind": kind, "id": id, "expected_version": version}
        async with self.pool.acquire() as conn, conn.transaction():
            target = await self._f3_target(conn, who, kind, id, lock=True)
            entity = target["legal_entity_id"]
            previous = await self._business_previous(conn, who, entity, action, key, request)
            if previous is not None:
                return previous
            if version != target["version"]:
                raise FinanceError("VERSION_CONFLICT", 409, "合规目标内容版本已变化")
            policy = await self._compliance_policy(conn, who.tenant_id, entity)
            assert policy is not None
            provider = await self._compliance_provider(conn, policy, "archive" if kind == "archive" else "tax")
            if provider["simulation"] != target["simulation"]:
                raise FinanceError("SIMULATION_MODE_CONFLICT", 409, "原件来源与查验环境不一致")
            bindings = {"target": target, "policy": policy, "provider": provider}
            if kind == "archive":
                original = await conn.fetchval("SELECT o.request FROM finance_archive a JOIN finance_compliance_operation o ON(o.tenant_id=a.tenant_id AND o.legal_entity_id=a.legal_entity_id AND o.id=a.operation_id) WHERE a.tenant_id=$1 AND a.legal_entity_id=$2 AND a.id=$3", who.tenant_id, entity, id)
                if original is None:
                    raise FinanceError("ARCHIVE_REQUEST_MISSING", 409, "原档案发送事实不存在")
                bindings["archive_request"] = decode(original)
            payload = {"id": "f3plan_" + identifier(), "version": 1, "tenant_id": who.tenant_id, "legal_entity_id": entity, "actor_id": who.user_id, "owner_id": target["owner_id"], "action": action, "target_type": kind, "target_id": id, "target_version": version, "parameters": {}, "simulation": target["simulation"], "expires_at": (datetime.now(UTC) + timedelta(hours=policy["plan_hours"])).isoformat(), "bindings": bindings, "required_approvals": [], "effects": ["查询原外部事实，受理不代表查验或封存成功"], "initial_blockers": []}
            plan = await self._f3_insert_plan(conn, payload)
            result = await self._f3_accept_operation(conn, plan)
            return await self._business_remember(conn, who, entity, action, key, request, result)

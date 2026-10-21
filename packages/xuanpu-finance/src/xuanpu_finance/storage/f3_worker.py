"""F3持久租约、确定性发送边界和只读恢复调度。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f3_reservations import assert_compliance_dimension
from xuanpu_finance.storage.f3_results import ComplianceResultStore


class ComplianceWorkerStore(ComplianceResultStore):
    """发送前在线复核，发送后崩溃、未知和not_found均禁止再次POST。"""

    async def claim_compliance(self, *, allow_dispatch: bool = True) -> dict[str, Any] | None:
        """SKIP LOCKED分配独立任务，过期dispatching恢复为query。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM finance_compliance_operation WHERE (lease_until IS NULL OR lease_until<now()) AND ((status IN ('accepted','running') AND $1) OR status='dispatching' OR (status IN ('awaiting_provider','outcome_unknown') AND (query_requested OR next_query_at<=now()))) ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED", allow_dispatch)
            if row is None:
                return None
            query = row["request"] is not None
            status = "outcome_unknown" if row["status"] == "dispatching" else row["status"] if query else "running"
            row = await conn.fetchrow("UPDATE finance_compliance_operation SET status=$2,version=version+1,lease_token=$3,lease_until=now()+interval '60 seconds',query_requested=false WHERE id=$1 RETURNING *", row["id"], status, identifier())
            assert row is not None
            await conn.execute("INSERT INTO finance_compliance_lease VALUES($1,$2,$3,$4)", row["lease_token"], row["tenant_id"], row["legal_entity_id"], row["id"])
            users = {row["owner_id"]}
            if not query:
                approvals = await conn.fetch("SELECT d.actor_id FROM finance_compliance_decision d JOIN finance_compliance_workflow w ON(w.tenant_id=d.tenant_id AND w.legal_entity_id=d.legal_entity_id AND w.id=d.workflow_id) WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.plan_id=$3 AND d.action='approve'", row["tenant_id"], row["legal_entity_id"], row["plan_id"])
                users.update(a["actor_id"] for a in approvals)
            return {"id": row["id"], "tenant_id": row["tenant_id"], "legal_entity_id": row["legal_entity_id"], "lease_token": row["lease_token"], "mode": "query" if query else "submit", "action": row["action"], "request": decode(row["request"]) if query else None, "request_template": decode(row["request_template"]), "actor_id": row["owner_id"], "provider": row["provider"], "configuration_version": row["configuration_version"], "authorization_user_ids": sorted(users), "simulation": row["simulation"]}

    async def renew_compliance(self, job: dict[str, Any]) -> None:
        """续租只接受当前仍未过期的fencing令牌。"""
        result = await self.pool.execute("UPDATE finance_compliance_operation SET lease_until=now()+interval '60 seconds' WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND lease_token=$4 AND lease_until>now() AND status IN ('running','dispatching','awaiting_provider','outcome_unknown')", job["tenant_id"], job["legal_entity_id"], job["id"], job["lease_token"])
        if result != "UPDATE 1":
            raise FinanceError("LEASE_LOST", 409, "合规任务租约已丢失")

    async def release_compliance(self, job: dict[str, Any]) -> None:
        """只释放该实例租约，不修改已固定发送事实。"""
        await self.pool.execute("UPDATE finance_compliance_operation SET lease_token=NULL,lease_until=NULL WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND lease_token=$4", job["tenant_id"], job["legal_entity_id"], job["id"], job["lease_token"])

    async def prepare_compliance_dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        """事务外preflight之后再在线复核，固定模板不得被worker替换。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._f3_worker_lock(conn, job)
            if row["request"] is not None:
                raise FinanceError("ALREADY_DISPATCHED", 409, "合规请求已经固定，只能查询")
            if row["status"] != "running" or job["mode"] != "submit":
                raise FinanceError("DISPATCH_NOT_ALLOWED", 409, "当前合规任务不能新发送")
            plan = await conn.fetchrow("SELECT * FROM finance_compliance_plan WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", row["tenant_id"], row["legal_entity_id"], row["plan_id"])
            assert plan is not None
            payload = decode(plan["data"])
            if payload["action"] in {"verify_invoice", "refresh_red_letter", "audit_archive"}:
                await self._f3_read_dispatch_checks(conn, row, plan)
            else:
                await self._f3_check_bindings(conn, plan)
                grant = await conn.fetchval("SELECT active FROM finance_compliance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3 AND plan_digest=$4", row["tenant_id"], row["legal_entity_id"], plan["id"], plan["digest"])
                if grant is not True:
                    raise FinanceError("ACTION_APPROVAL_REQUIRED", 409, "合规批准授权已失效")
            await assert_compliance_dimension(conn, plan, exclude_operation_id=row["id"])
            request = self._f3_request(plan, row["id"], row["external_request_id"])
            if request != decode(row["request_template"]) or request != job["request_template"]:
                raise FinanceError("REQUEST_TEMPLATE_CHANGED", 409, "固定发送模板不符")
            await conn.execute("UPDATE finance_compliance_operation SET status='dispatching',version=version+1,request=$4::jsonb WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", row["tenant_id"], row["legal_entity_id"], row["id"], json.dumps(request))
            job["request"] = request
            return request

    async def _f3_read_dispatch_checks(self, conn: Any, row: Any, plan: Any) -> None:
        """只读外查仍验证来源、当前授权与渠道墓碑，但不需人工审批。"""
        payload = decode(plan["data"])
        if plan["expires_at"] <= datetime.now(UTC):
            raise FinanceError("PLAN_EXPIRED", 409, "合规外查计划已经过期")
        who = FinancePrincipal(row["tenant_id"], row["owner_id"])
        target = await self._f3_target(conn, who, row["target_type"], row["target_id"])
        if target["version"] != payload["target_version"] or target["simulation"] != row["simulation"]:
            raise FinanceError("PLAN_BINDINGS_CHANGED", 409, "外查目标内容或来源已变化")
        policy = await self._compliance_policy(conn, row["tenant_id"], row["legal_entity_id"])
        assert policy is not None
        provider = await self._compliance_provider(conn, policy, "archive" if row["action"] == "audit_archive" else "tax")
        if request_digest(policy) != request_digest(payload["bindings"]["policy"]) or request_digest(provider) != request_digest(payload["bindings"]["provider"]):
            raise FinanceError("PLAN_BINDINGS_CHANGED", 409, "外查制度或渠道已变化")

    async def reject_compliance_before_dispatch(self, job: dict[str, Any], code: str) -> None:
        """明确未发送才可失败；已发送操作不能被误判为安全重试。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._f3_worker_lock(conn, job)
            if row["request"] is not None:
                raise FinanceError("ALREADY_DISPATCHED", 409, "已发送合规请求只能查询")
            await conn.execute("UPDATE finance_compliance_operation SET status='failed',version=version+1,business_result='failed',last_error=$4 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", row["tenant_id"], row["legal_entity_id"], row["id"], code)

    async def defer_compliance(self, job: dict[str, Any], code: str, *, uncertain: bool) -> None:
        """未知结果按1/5/15/60分钟核对，四次后只待人工，不重发。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._f3_worker_lock(conn, job)
            if row["request"] is None and uncertain:
                raise FinanceError("REQUEST_NOT_PREPARED", 409, "未知结果必须关联实际固定请求")
            status = "running" if row["request"] is None else "outcome_unknown" if uncertain else "awaiting_provider"
            count = row["query_count"] + (1 if job["mode"] == "query" else 0)
            delay = (60, 300, 900, 3600)[min(count, 3)]
            await conn.execute("UPDATE finance_compliance_operation SET status=$4,version=version+1,last_error=$5,query_count=$6,next_query_at=CASE WHEN $6<4 THEN now()+($7::integer*interval '1 second') ELSE NULL END WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", row["tenant_id"], row["legal_entity_id"], row["id"], status, code, count, delay)
            if count >= 4:
                await self._f3_exception(conn, row, "MANUAL_RECONCILIATION_REQUIRED", {"code": code})

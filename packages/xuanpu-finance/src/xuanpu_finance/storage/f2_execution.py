"""真实人工决定、专用授权和外部操作的原子受理。"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f2_plans import EXTERNAL_ACTIONS, SettlementPlanStore

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ActionExecution
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class SettlementExecutionStore(SettlementPlanStore):
    """人工受理与资金占用同事务，渠道结果由独立worker核实。"""

    async def execute_finance_action(
        self, who: FinancePrincipal, body: ActionExecution, key: str
    ) -> dict[str, Any]:
        """外部动作只能由原经办真人执行，模型身份没有提升路径。"""
        if who.origin != "human":
            raise FinanceError("HUMAN_REQUIRED", 403, "所有业务和外部执行决定必须是真实人工")
        async with self.pool.acquire() as conn:
            plan = await self._plan_row(conn, who, body.plan_id)
            payload = decode(plan["data"])
        if payload["action"] in EXTERNAL_ACTIONS or payload["target_type"] == "action_plan":
            return await self._execute_settlement_plan(who, body, key)
        return await super().execute_finance_action(who, body, key)

    async def _execute_settlement_plan(
        self, who: FinancePrincipal, body: ActionExecution, key: str
    ) -> dict[str, Any]:
        """固定计划只能受理一次，换幂等键也只能恢复同一Operation。"""
        request = body.model_dump(mode="json")
        async with self.pool.acquire() as conn, conn.transaction():
            plan = await self._plan_row(conn, who, body.plan_id)
            doc = await self._claim_row(conn, who, plan["target_id"], lock=True)
            old = await self._business_previous(
                conn, who, doc["legal_entity_id"], "executeFinanceAction", key, request
            )
            if old is not None:
                return old
            if plan["owner_id"] != who.user_id:
                raise FinanceError("PLAN_OWNER_REQUIRED", 403, "只有原计划经办人可明确执行")
            if plan["digest"] != body.plan_digest:
                raise FinanceError("PLAN_DIGEST_MISMATCH", 409, "原计划摘要不符")
            payload = decode(plan["data"])
            if payload["target_type"] == "action_plan":
                existing = await conn.fetchval(
                    "SELECT data FROM finance_business_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3",
                    who.tenant_id,
                    doc["legal_entity_id"],
                    plan["id"],
                )
                if existing is not None:
                    return await self._business_remember(
                        conn,
                        who,
                        doc["legal_entity_id"],
                        "executeFinanceAction",
                        key,
                        request,
                        decode(existing),
                    )
                blockers = await self._plan_review_blockers(conn, who, plan, lock=True)
                if blockers:
                    self._raise_blocker(blockers)
                await self._apply_plan_review(conn, who, doc, plan, payload)
                result = await self._business_operation(
                    conn,
                    who,
                    doc["legal_entity_id"],
                    "action_plan",
                    payload["target_id"],
                    plan_id=plan["id"],
                )
            else:
                existing = await conn.fetchrow(
                    "SELECT * FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3",
                    who.tenant_id,
                    doc["legal_entity_id"],
                    plan["id"],
                )
                if existing is not None:
                    return await self._business_remember(
                        conn,
                        who,
                        doc["legal_entity_id"],
                        "executeFinanceAction",
                        key,
                        request,
                        self._settlement_operation_view(existing),
                    )
                blockers = await self._external_plan_blockers(conn, who, plan, lock=True)
                if blockers:
                    self._raise_blocker(blockers)
                result = await self._accept_external_action(conn, who, doc, plan, payload)
            return await self._business_remember(
                conn, who, doc["legal_entity_id"], "executeFinanceAction", key, request, result
            )

    def _raise_blocker(self, blockers: list[str]) -> None:
        """只抛出精确领域首阻断，不把尚未批准伪装为任务成功。"""
        code, _, reason = blockers[0].partition(":")
        raise FinanceError(code, 409, reason)

    async def _apply_plan_review(
        self, conn: Connection, who: FinancePrincipal, doc: Any, plan: Any, payload: dict[str, Any]
    ) -> None:
        """追加实际人工决定，最后一步批准才签发匹配原摘要的grant。"""
        original = await self._plan_row(conn, who, payload["target_id"])
        workflow, steps = await self._plan_workflow(conn, original, lock=True)
        active = next(s for s in steps if s["status"] == "pending")
        await self._decision(conn, who, doc, workflow, payload, active["id"])
        approved = payload["action"] == "approve"
        await conn.execute(
            "UPDATE finance_workflow_step SET status=$4,version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
            who.tenant_id,
            doc["legal_entity_id"],
            active["id"],
            "approved" if approved else "rejected",
        )
        waiting = [s for s in steps if s["status"] == "waiting"]
        status = "under_review" if approved and waiting else "approved" if approved else "rejected"
        if approved and waiting:
            await conn.execute(
                "UPDATE finance_workflow_step SET status='pending',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
                who.tenant_id,
                doc["legal_entity_id"],
                waiting[0]["id"],
            )
        elif not approved:
            await conn.execute(
                "UPDATE finance_workflow_step SET status='cancelled',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 AND status='waiting'",
                who.tenant_id,
                doc["legal_entity_id"],
                workflow["id"],
            )
        await conn.execute(
            "UPDATE finance_workflow SET status=$4,version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
            who.tenant_id,
            doc["legal_entity_id"],
            workflow["id"],
            status,
        )
        if status == "approved":
            data = decode(original["data"])
            await conn.execute(
                "INSERT INTO finance_execution_grant(tenant_id,legal_entity_id,id,document_id,workflow_id,scope,target_version,policy_version,policy_digest,policy_generation,plan_id,plan_digest,amount_limit,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
                who.tenant_id,
                doc["legal_entity_id"],
                identifier(),
                doc["id"],
                workflow["id"],
                data["action"],
                doc["version"],
                workflow["policy_version"],
                workflow["policy_digest"],
                workflow["policy_generation"],
                original["id"],
                original["digest"],
                Decimal(data["parameters"]["amount"]),
                original["expires_at"],
            )
        await self._event(
            conn,
            doc,
            "settlement.plan_" + status,
            {
                "plan_id": original["id"],
                "decision_plan_id": plan["id"],
                "actor_id": who.user_id,
                "workflow_id": workflow["id"],
            },
        )

    async def _accept_external_action(
        self, conn: Connection, who: FinancePrincipal, doc: Any, plan: Any, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """单据锁下新建唯一队列及pending记录，外部成功尚未发生。"""
        bindings = payload["bindings"]
        provider = bindings["provider"]
        op_id = "f2op_" + identifier()
        record_id = identifier()
        amount = Decimal(payload["parameters"]["amount"])
        row = await conn.fetchrow(
            "INSERT INTO finance_settlement_operation(tenant_id,legal_entity_id,id,owner_id,document_id,plan_id,action,target_id,amount,simulation,provider,configuration_version,adapter_version,external_request_id,document_kind) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING *",
            who.tenant_id,
            doc["legal_entity_id"],
            op_id,
            who.user_id,
            doc["id"],
            plan["id"],
            payload["action"],
            payload["target_id"],
            amount,
            doc["simulation"],
            provider["provider"],
            provider["configuration_version"],
            provider["adapter_version"],
            "f2req_" + identifier(),
            doc["document_kind"],
        )
        if payload["action"] == "execute_payment":
            account = bindings["payee"]
            data = {
                "payee_ref": account["ref"],
                "account_name": account["account_name"],
                "masked_account": account["masked_account"],
            }
            await conn.execute(
                "INSERT INTO finance_payment(tenant_id,legal_entity_id,id,document_id,operation_id,amount,simulation,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)",
                who.tenant_id,
                doc["legal_entity_id"],
                record_id,
                doc["id"],
                op_id,
                amount,
                doc["simulation"],
                json.dumps(data),
            )
        else:
            original = payload["target_id"] if payload["action"] == "reverse_posting" else None
            data = {
                "ledger_id": bindings["ledger_id"],
                "accounting_period": bindings["accounting_period"],
            }
            await conn.execute(
                "INSERT INTO finance_posting(tenant_id,legal_entity_id,id,document_id,operation_id,amount,simulation,original_posting_id,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)",
                who.tenant_id,
                doc["legal_entity_id"],
                record_id,
                doc["id"],
                op_id,
                amount,
                doc["simulation"],
                original,
                json.dumps(data),
            )
        await self._event(
            conn,
            doc,
            "settlement.accepted",
            {
                "operation_id": op_id,
                "plan_id": plan["id"],
                "action": payload["action"],
                "amount": payload["parameters"]["amount"],
                "simulation": doc["simulation"],
            },
        )
        result = self._settlement_operation_view(row)
        await self._settlement_outbox(conn, row, result)
        return result

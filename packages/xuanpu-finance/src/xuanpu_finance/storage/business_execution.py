"""人工执行不可变计划，并生成独立本地Operation和通知Outbox。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.business_workflow import WorkflowStore
from xuanpu_finance.storage.common import decode, identifier

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ActionExecution
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class ExecutionStore(WorkflowStore):
    """业务回执不进入F0 worker，待审业务通过持久队列继续。"""

    async def execute_finance_action(
        self, who: FinancePrincipal, body: ActionExecution, key: str
    ) -> dict[str, Any]:
        """仅可信人工来源可执行，换键重复计划只返回原操作。"""
        if who.origin != "human":
            raise FinanceError(
                "HUMAN_REQUIRED", 403, "提交、批准、驳回和撤回必须由真实员工明确决定"
            )
        request = body.model_dump(mode="json")
        async with self.pool.acquire() as conn, conn.transaction():
            plan = await self._plan_row(conn, who, body.plan_id)
            claim = await self._claim_row(conn, who, plan["target_id"], lock=True)
            entity = claim["legal_entity_id"]
            previous = await self._business_previous(
                conn, who, entity, "executeFinanceAction", key, request
            )
            if previous is not None:
                return previous
            if body.plan_digest != plan["digest"]:
                raise FinanceError("PLAN_DIGEST_MISMATCH", 409, "计划摘要不符")
            existing = await conn.fetchval(
                "SELECT data FROM finance_business_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3",
                who.tenant_id,
                entity,
                plan["id"],
            )
            if existing is not None:
                return await self._business_remember(
                    conn, who, entity, "executeFinanceAction", key, request, decode(existing)
                )
            blockers = await self._plan_blockers(conn, who, plan, claim, lock=True)
            if blockers:
                code, _, reason = blockers[0].partition(":")
                raise FinanceError(code, 409, reason)
            payload = decode(plan["data"])
            await self._apply_action(conn, who, claim, payload)
            result = await self._business_operation(
                conn, who, entity, claim["document_kind"], claim["id"], plan_id=plan["id"]
            )
            return await self._business_remember(
                conn, who, entity, "executeFinanceAction", key, request, result
            )

    async def _business_operation(
        self,
        conn: Connection,
        who: FinancePrincipal,
        entity: str,
        kind: str,
        target: str,
        *,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        """本地事务成功与业务批准分开，通知通道缺省保留pending。"""
        result = {
            "id": "f1op_" + identifier(),
            "version": 1,
            "status": "succeeded",
            "business_result": "succeeded",
            "trace_id": identifier(),
            "items": [],
            "result_refs": [],
            "resources": [{"kind": kind, "id": target}],
        }
        await conn.execute(
            "INSERT INTO finance_business_operation(tenant_id,legal_entity_id,id,owner_id,plan_id,data) VALUES($1,$2,$3,$4,$5,$6::jsonb)",
            who.tenant_id,
            entity,
            result["id"],
            who.user_id,
            plan_id,
            json.dumps(result),
        )
        await conn.execute(
            "INSERT INTO finance_outbox(tenant_id,legal_entity_id,id,operation_id,data) VALUES($1,$2,$3,$4,$5::jsonb)",
            who.tenant_id,
            entity,
            identifier(),
            result["id"],
            json.dumps({"resources": result["resources"], "plan_id": plan_id}),
        )
        return result

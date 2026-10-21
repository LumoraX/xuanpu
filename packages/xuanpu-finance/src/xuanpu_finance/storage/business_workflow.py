"""持久顺序审批：每一步只接受当前实际分派的人工决定。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from xuanpu_finance.expenses.models import ActionParameters, ActionPreview
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.business_base import money
from xuanpu_finance.storage.business_plans import PlanStore
from xuanpu_finance.storage.common import encode_cursor, identifier, page, timestamp_cursor

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class WorkflowStore(PlanStore):
    """工作流状态与额度、决定和事件始终同事务推进。"""

    async def _apply_action(
        self, conn: Connection, who: FinancePrincipal, claim: Any, payload: dict[str, Any]
    ) -> None:
        """经再次验证后的动作只执行对应领域转换。"""
        action = payload["action"]
        if action == "submit_claim":
            await self._submit_claim(conn, who, claim)
            return
        workflow, steps = await self._workflow_state(conn, claim, lock=True)
        assert workflow is not None
        await self._lock_claim_invoices(conn, claim)
        if action == "approve":
            await self._approve_step(conn, who, claim, workflow, steps, payload)
        else:
            await self._close_workflow(conn, who, claim, workflow, steps, payload)

    async def _lock_claim_invoices(self, conn: Connection, claim: Any) -> None:
        """单据、流程后按票ID排序加锁，提交和释放使用同一锁序。"""
        await conn.fetch(
            "SELECT i.id FROM finance_invoice i WHERE i.tenant_id=$1 AND i.legal_entity_id=$2 AND EXISTS(SELECT 1 FROM finance_allocation a WHERE a.tenant_id=i.tenant_id AND a.legal_entity_id=i.legal_entity_id AND a.invoice_id=i.id AND a.document_id=$3 AND a.state IN ('reserved','proposed')) ORDER BY i.id FOR UPDATE",
            claim["tenant_id"],
            claim["legal_entity_id"],
            claim["id"],
        )

    async def _submit_claim(self, conn: Connection, who: FinancePrincipal, claim: Any) -> None:
        """原子预留全部分配并冻结审批链，提交成功不表示已获批准。"""
        policy = await self._policy(conn, who.tenant_id, claim["legal_entity_id"])
        assert policy is not None
        steps = await self._applicable_steps(conn, claim, policy)
        await conn.execute(
            "UPDATE finance_allocation SET state='reserved',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state='proposed'",
            who.tenant_id,
            claim["legal_entity_id"],
            claim["id"],
        )
        claim = await self._bump_claim(conn, claim, status="under_review")
        due = datetime.now(UTC) + timedelta(hours=policy.get("approval_due_hours", 72))
        workflow_id = identifier()
        await conn.execute(
            "INSERT INTO finance_workflow(tenant_id,legal_entity_id,id,document_id,target_version,status,policy_version,policy_digest,due_at,policy_generation) VALUES($1,$2,$3,$4,$5,'under_review',$6,$7,$8,$9)",
            who.tenant_id,
            claim["legal_entity_id"],
            workflow_id,
            claim["id"],
            claim["version"],
            policy["version"],
            policy["digest"],
            due,
            policy["generation"],
        )
        for position, step in enumerate(steps):
            await conn.execute(
                "INSERT INTO finance_workflow_step(tenant_id,legal_entity_id,id,workflow_id,position,approver_user_id,name,status,authority_generation) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                who.tenant_id,
                claim["legal_entity_id"],
                identifier(),
                workflow_id,
                position,
                step["approver_user_id"],
                step["name"],
                "pending" if position == 0 else "waiting",
                await self._current_approver(conn, claim, step["approver_user_id"]),
            )
        await self._save_history(conn, claim)
        await self._event(
            conn,
            claim,
            "expense.submitted",
            {"version": claim["version"], "workflow_id": workflow_id},
        )
        await self._event(conn, claim, "expense.under_review", {"workflow_id": workflow_id})

    async def _decision(
        self,
        conn: Connection,
        who: FinancePrincipal,
        claim: Any,
        workflow: Any,
        payload: dict[str, Any],
        step_id: str | None,
    ) -> None:
        """追加不可变人工决定，保留参数和原计划摘要关联。"""
        await conn.execute(
            "INSERT INTO finance_decision(tenant_id,legal_entity_id,id,workflow_id,step_id,actor_id,action,data) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)",
            who.tenant_id,
            claim["legal_entity_id"],
            identifier(),
            workflow["id"],
            step_id,
            who.user_id,
            payload["action"],
            json.dumps(
                {
                    "plan_id": payload["id"],
                    "parameters": payload["parameters"],
                    "target_version": payload["target_version"],
                    "origin": who.origin,
                }
            ),
        )

    async def _approve_step(
        self,
        conn: Connection,
        who: FinancePrincipal,
        claim: Any,
        workflow: Any,
        steps: list[Any],
        payload: dict[str, Any],
    ) -> None:
        """中间决定只改变流程和步骤版本，最终决定才增加单据版本。"""
        step = next(step for step in steps if step["status"] == "pending")
        await self._decision(conn, who, claim, workflow, payload, step["id"])
        await conn.execute(
            "UPDATE finance_workflow_step SET status='approved',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
            who.tenant_id,
            claim["legal_entity_id"],
            step["id"],
        )
        waiting = [entry for entry in steps if entry["status"] == "waiting"]
        if waiting:
            await conn.execute(
                "UPDATE finance_workflow_step SET status='pending',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
                who.tenant_id,
                claim["legal_entity_id"],
                waiting[0]["id"],
            )
        else:
            claim = await self._bump_claim(conn, claim, status="approved")
            await conn.execute(
                "INSERT INTO finance_execution_grant(tenant_id,legal_entity_id,id,document_id,workflow_id,scope,target_version,policy_version,policy_digest,policy_generation) VALUES($1,$2,$3,$4,$5,'expense_approved',$6,$7,$8,$9)",
                who.tenant_id,
                claim["legal_entity_id"],
                identifier(),
                claim["id"],
                workflow["id"],
                claim["version"],
                workflow["policy_version"],
                workflow["policy_digest"],
                workflow["policy_generation"],
            )
        await conn.execute(
            "UPDATE finance_workflow SET version=version+1,status=$4 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
            who.tenant_id,
            claim["legal_entity_id"],
            workflow["id"],
            "under_review" if waiting else "approved",
        )
        if not waiting:
            await self._save_history(conn, claim)
        await self._event(
            conn,
            claim,
            "expense.step_approved" if waiting else "expense.approved",
            {
                "workflow_id": workflow["id"],
                "workflow_step_id": step["id"],
                "actor_id": who.user_id,
            },
        )

    async def _close_workflow(
        self,
        conn: Connection,
        who: FinancePrincipal,
        claim: Any,
        workflow: Any,
        steps: list[Any],
        payload: dict[str, Any],
    ) -> None:
        """驳回或撤回取消余步并一次释放，历史决定和原件全部保留。"""
        action = payload["action"]
        state = "rejected" if action == "reject" else "withdrawn"
        active = next((step for step in steps if step["status"] == "pending"), None)
        step_id = active["id"] if action == "reject" and active is not None else None
        await self._decision(conn, who, claim, workflow, payload, step_id)
        if step_id is not None:
            await conn.execute(
                "UPDATE finance_workflow_step SET status='rejected',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
                who.tenant_id,
                claim["legal_entity_id"],
                step_id,
            )
        await conn.execute(
            "UPDATE finance_workflow_step SET status='cancelled',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 AND status IN ('pending','waiting')",
            who.tenant_id,
            claim["legal_entity_id"],
            workflow["id"],
        )
        await conn.execute(
            "UPDATE finance_workflow SET status=$4,version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
            who.tenant_id,
            claim["legal_entity_id"],
            workflow["id"],
            state,
        )
        await conn.execute(
            "UPDATE finance_allocation SET state='released',version=version+1 WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND state IN ('proposed','reserved')",
            who.tenant_id,
            claim["legal_entity_id"],
            claim["id"],
        )
        await conn.execute(
            "UPDATE finance_execution_grant SET active=false WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3",
            who.tenant_id,
            claim["legal_entity_id"],
            claim["id"],
        )
        claim = await self._bump_claim(conn, claim, status=state)
        await self._save_history(conn, claim)
        await self._event(
            conn,
            claim,
            "expense." + state,
            {"workflow_id": workflow["id"], "reason": payload["parameters"]["reason"]},
        )

    async def list_review_queue(
        self, who: FinancePrincipal, entity: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """列出本人当前实际分派的步骤，重启后仍可人工继续。"""
        scope = request_digest([who.tenant_id, who.user_id, entity, "review_queue"])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            role = await self._role(conn, who, entity)
            if role not in {"approver", "reviewer"}:
                return page([], 0, None)
            where = "s.tenant_id=$1 AND s.legal_entity_id=$2 AND s.approver_user_id=$3 AND s.status='pending' AND w.status='under_review'"
            joins = " FROM finance_workflow_step s JOIN finance_workflow w ON(w.tenant_id=s.tenant_id AND w.legal_entity_id=s.legal_entity_id AND w.id=s.workflow_id) JOIN finance_expense_claim c ON(c.tenant_id=w.tenant_id AND c.legal_entity_id=w.legal_entity_id AND c.id=w.document_id) WHERE "
            args = (who.tenant_id, entity, who.user_id)
            total = await conn.fetchval("SELECT count(*)" + joins + where, *args)
            rows = await conn.fetch(
                "SELECT c.*,s.id AS step_id,s.version AS step_version,s.approver_user_id,w.id AS workflow_id,w.version AS workflow_version,w.due_at"
                + joins
                + where
                + " AND ($4::timestamptz IS NULL OR (c.created_at,s.id)>($4,$5::text)) ORDER BY c.created_at,s.id LIMIT $6",
                *args,
                stamp,
                last,
                limit + 1,
            )
            items = [await self._review_item(conn, who, row) for row in rows[:limit]]
            cursor_next = (
                encode_cursor(
                    scope, [rows[limit - 1]["created_at"].isoformat(), rows[limit - 1]["step_id"]]
                )
                if len(rows) > limit
                else None
            )
            return page(items, total, cursor_next)

    async def _review_item(
        self, conn: Connection, who: FinancePrincipal, row: Any
    ) -> dict[str, Any]:
        """待办展示实时制度阻断和原始证据，不因超时自动作出决定。"""
        body = ActionPreview(
            action="approve",
            target_type="expense_claim",
            target_id=row["id"],
            expected_version=row["version"],
            parameters=ActionParameters(workflow_step_id=row["step_id"]),
        )
        blockers, _ = await self._action_check(conn, who, row, body, lock=False)
        return {
            "workflow_step_id": row["step_id"],
            "target_id": row["id"],
            "target_type": "expense_claim",
            "target_version": row["version"],
            "summary": decode_content(row)["reason"],
            "total": money(row["total"]),
            "evidence": await self._claim_evidence(conn, row),
            "workflow_id": row["workflow_id"],
            "workflow_version": row["workflow_version"],
            "step_version": row["step_version"],
            "approver_user_id": row["approver_user_id"],
            "status": "pending",
            "due_at": row["due_at"].isoformat(),
            "blockers": blockers,
        }


def decode_content(row: Any) -> dict[str, Any]:
    """将单据正文转换为显示摘要。"""
    result: dict[str, Any] = (
        json.loads(row["content"]) if isinstance(row["content"], str) else dict(row["content"])
    )
    return result

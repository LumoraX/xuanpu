"""付款/记账不可变计划绑定与实时执行资格复核。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ActionPreview
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, identifier
from xuanpu_finance.storage.f2_checks import SettlementCheckStore

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection

EXTERNAL_ACTIONS = frozenset({"execute_payment", "submit_posting", "reverse_posting"})


class SettlementPlanStore(SettlementCheckStore):
    """专用计划的原意图不随着后续人工决定而改变。"""

    async def preview_finance_action(
        self, who: FinancePrincipal, body: ActionPreview, key: str
    ) -> dict[str, Any]:
        """F1动作继续原实现，外部动作和计划批准各有固定目标。"""
        if body.target_type == "action_plan":
            return await self._preview_plan_review(who, body, key)
        if body.action not in EXTERNAL_ACTIONS:
            return await super().preview_finance_action(who, body, key)
        request = body.model_dump(mode="json", exclude_none=True)
        async with self.pool.acquire() as conn, conn.transaction():
            doc = await self._external_document(conn, who, body, lock=True)
            old = await self._business_previous(
                conn, who, doc["legal_entity_id"], "previewFinanceAction", key, request
            )
            if old is not None:
                return await self._plan_view(conn, who, await self._plan_row(conn, who, old["id"]))
            blockers: list[str] = []
            bindings: dict[str, Any] = {}
            steps: list[dict[str, Any]] = []
            try:
                bindings, steps = await self._external_bindings(conn, who, doc, body, lock=False)
            except FinanceError as exc:
                blockers = [f"{exc.code}:{exc.message}"]
            workflow_id = identifier()
            hours = bindings.get("settlement_policy", {}).get("plan_hours", 24)
            payload = {
                "id": identifier(),
                "version": 1,
                "legal_entity_id": doc["legal_entity_id"],
                "action": body.action,
                "target_type": body.target_type,
                "target_id": body.target_id,
                "target_version": body.expected_version,
                "parameters": request["parameters"],
                "expires_at": (datetime.now(UTC) + timedelta(hours=hours)).isoformat(),
                "required_approvals": [s["approver_user_id"] for s in steps],
                "effects": [
                    {
                        "execute_payment": "按固定金额和已核实账户发起付款，受理不代表到账",
                        "submit_posting": "向公司固定账套提交入账，以正式凭证回执为准",
                        "reverse_posting": "按固定金额冲回原凭证，不改写原始入账事实",
                    }[body.action]
                ],
                "actor_id": who.user_id,
                "tenant_id": who.tenant_id,
                "owner_id": doc["owner_id"],
                "simulation": doc["simulation"],
                "bindings": bindings,
                "workflow_id": workflow_id,
            }
            digest = request_digest(payload)
            await self._insert_settlement_plan(conn, who, doc, payload, digest)
            if not blockers:
                await self._create_plan_workflow(conn, who, doc, payload, digest, steps)
                blockers = ["ACTION_APPROVAL_REQUIRED:该固定行动计划尚需专用人工批准"]
            return await self._business_remember(
                conn,
                who,
                doc["legal_entity_id"],
                "previewFinanceAction",
                key,
                request,
                self._public_plan(payload, digest, blockers),
            )

    async def _insert_settlement_plan(
        self,
        conn: Connection,
        who: FinancePrincipal,
        doc: Any,
        payload: dict[str, Any],
        digest: str,
    ) -> None:
        """父单据FK保持原表隔离，真实目标固定在不可变正文。"""
        await conn.execute(
            "INSERT INTO finance_action_plan VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)",
            who.tenant_id,
            doc["legal_entity_id"],
            payload["id"],
            who.user_id,
            doc["id"],
            digest,
            datetime.fromisoformat(payload["expires_at"]),
            json.dumps(payload),
        )

    async def _external_document(
        self, conn: Connection, who: FinancePrincipal, body: ActionPreview, *, lock: bool = False
    ) -> Any:
        """冲回先解析原凭证，再以父单据为统一锁和权限边界。"""
        if body.target_type != "posting":
            return await self._claim_row(conn, who, body.target_id, lock=lock)
        posting = await conn.fetchrow(
            "SELECT * FROM finance_posting WHERE tenant_id=$1 AND id=$2",
            who.tenant_id,
            body.target_id,
        )
        if posting is None:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "原记账记录不存在")
        return await self._claim_row(conn, who, posting["document_id"], lock=lock)

    async def _plan_row(self, conn: Connection, who: FinancePrincipal, id: str) -> Any:
        """原外部计划可由实际审核人恢复，派生决定计划仍仅原actor。"""
        row = await conn.fetchrow(
            "SELECT * FROM finance_action_plan WHERE tenant_id=$1 AND id=$2", who.tenant_id, id
        )
        if row is None:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "行动计划不存在")
        payload = decode(row["data"])
        if payload["action"] not in EXTERNAL_ACTIONS:
            return await super()._plan_row(conn, who, id)
        await self._claim_row(conn, who, row["target_id"])
        assigned = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_workflow w JOIN finance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.target_type='action_plan' AND w.target_id=$3 AND s.approver_user_id=$4)",
            who.tenant_id,
            row["legal_entity_id"],
            id,
            who.user_id,
        )
        if (
            row["owner_id"] != who.user_id
            and not assigned
            and await self._role(conn, who, row["legal_entity_id"]) != "reviewer"
        ):
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "行动计划不存在")
        return row

    def _public_plan(
        self, payload: dict[str, Any], digest: str, blockers: list[str]
    ) -> dict[str, Any]:
        """原计划可恢复脱敏影响和经办人，绝不泄漏渠道账户令牌。"""
        result = super()._public_plan(payload, digest, blockers)
        if payload["action"] in EXTERNAL_ACTIONS:
            result["prepared_by"] = payload["actor_id"]
            bindings = payload["bindings"]
            if "payee" in bindings:
                result["payee"] = {
                    k: bindings["payee"][k] for k in ("ref", "account_name", "masked_account")
                }
            if "ledger_id" in bindings:
                result.update(
                    {
                        "ledger_id": bindings["ledger_id"],
                        "accounting_period": bindings["accounting_period"],
                    }
                )
        return result

    async def _plan_view(self, conn: Connection, who: FinancePrincipal, row: Any) -> dict[str, Any]:
        """专用批准变化只改变工作流投影，不修改原计划摘要。"""
        payload = decode(row["data"])
        if payload["action"] in EXTERNAL_ACTIONS:
            blockers = await self._external_plan_blockers(conn, who, row, lock=False)
            result = self._public_plan(payload, row["digest"], blockers)
            workflow, steps = await self._plan_workflow(conn, row)
            if workflow is not None:
                result["workflow"] = self._plan_workflow_view(workflow, steps)
            return result
        if payload["target_type"] == "action_plan":
            blockers = await self._plan_review_blockers(conn, who, row)
            return self._public_plan(payload, row["digest"], blockers)
        return await super()._plan_view(conn, who, row)

    async def _external_plan_blockers(
        self,
        conn: Connection,
        who: FinancePrincipal,
        row: Any,
        *,
        lock: bool,
        approval: bool = False,
        executing_id: str | None = None,
    ) -> list[str]:
        """审批与发送都在线复核固定原计划，失效计划不能被重新批准。"""
        payload = decode(row["data"])
        if row["expires_at"] <= datetime.now(UTC):
            return ["PLAN_EXPIRED:原行动计划已过期"]
        doc = await self._claim_row(conn, who, row["target_id"], lock=lock)
        actor = FinancePrincipal(who.tenant_id, row["owner_id"], origin="human")
        body = ActionPreview(
            action=payload["action"],
            target_type=payload["target_type"],
            target_id=payload["target_id"],
            expected_version=payload["target_version"],
            parameters=payload["parameters"],
        )
        try:
            bindings, _ = await self._external_bindings(
                conn, actor, doc, body, lock=lock, exclude_operation_id=executing_id
            )
            if bindings != payload["bindings"]:
                return ["PLAN_INVALIDATED:原单据、账户、制度、人员或渠道版本已变化"]
        except FinanceError as exc:
            return [f"{exc.code}:{exc.message}"]
        workflow, _ = await self._plan_workflow(conn, row, lock=lock)
        if workflow is None:
            return ["PLAN_INVALIDATED:原专用审批流程未建立"]
        if workflow["status"] == "rejected":
            return ["PLAN_REJECTED:原计划已被驳回"]
        used = await conn.fetchval(
            "SELECT id FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3",
            who.tenant_id,
            row["legal_entity_id"],
            row["id"],
        )
        if used and used != executing_id:
            return ["PLAN_ALREADY_EXECUTED:该原计划已经执行"]
        if approval:
            return []
        grant = await conn.fetchrow(
            "SELECT * FROM finance_execution_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3 AND plan_digest=$4 AND scope=$5 AND active AND expires_at>now()",
            who.tenant_id,
            row["legal_entity_id"],
            row["id"],
            row["digest"],
            payload["action"],
        )
        if workflow["status"] != "approved" or grant is None:
            return ["ACTION_APPROVAL_REQUIRED:原计划尚未获得有效专用批准"]
        return []

    async def _create_plan_workflow(
        self,
        conn: Connection,
        who: FinancePrincipal,
        doc: Any,
        payload: dict[str, Any],
        digest: str,
        steps: list[dict[str, Any]],
    ) -> None:
        """创建原计划唯一持久专用审批，计划本身始终version=1。"""
        policy = payload["bindings"]["settlement_policy"]
        await conn.execute(
            "INSERT INTO finance_workflow(tenant_id,legal_entity_id,id,document_id,target_version,status,policy_version,policy_digest,due_at,policy_generation,target_type,target_digest,target_id) VALUES($1,$2,$3,$4,1,'under_review',$5,$6,$7,$8,'action_plan',$9,$10)",
            who.tenant_id,
            doc["legal_entity_id"],
            payload["workflow_id"],
            doc["id"],
            policy["version"],
            policy["digest"],
            datetime.fromisoformat(payload["expires_at"]),
            policy["generation"],
            digest,
            payload["id"],
        )
        for position, step in enumerate(steps):
            await conn.execute(
                "INSERT INTO finance_workflow_step(tenant_id,legal_entity_id,id,workflow_id,position,approver_user_id,name,status,authority_generation) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                who.tenant_id,
                doc["legal_entity_id"],
                identifier(),
                payload["workflow_id"],
                position,
                step["approver_user_id"],
                step["name"],
                "pending" if position == 0 else "waiting",
                await self._current_approver(conn, doc, step["approver_user_id"]),
            )
        await self._event(
            conn,
            doc,
            "settlement.plan_created",
            {"plan_id": payload["id"], "digest": digest, "workflow_id": payload["workflow_id"]},
        )

    async def _plan_workflow(
        self, conn: Connection, plan: Any, *, lock: bool = False
    ) -> tuple[Any, list[Any]]:
        """行动计划工作流独立于同一单据的业务审批。"""
        row = await conn.fetchrow(
            "SELECT * FROM finance_workflow WHERE tenant_id=$1 AND legal_entity_id=$2 AND target_type='action_plan' AND target_id=$3"
            + (" FOR UPDATE" if lock else ""),
            plan["tenant_id"],
            plan["legal_entity_id"],
            plan["id"],
        )
        if row is None:
            return None, []
        steps = await conn.fetch(
            "SELECT * FROM finance_workflow_step WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 ORDER BY position"
            + (" FOR UPDATE" if lock else ""),
            plan["tenant_id"],
            plan["legal_entity_id"],
            row["id"],
        )
        return row, list(steps)

    def _plan_workflow_view(self, workflow: Any, steps: list[Any]) -> dict[str, Any]:
        """人工入口从同一持久流程恢复实际分派人和步骤。"""
        return {
            **{
                k: workflow[k]
                for k in ("id", "version", "status", "target_version", "policy_version")
            },
            "due_at": workflow["due_at"].isoformat(),
            "provider": "internal",
            "current_step_id": next((s["id"] for s in steps if s["status"] == "pending"), None),
            "steps": [
                {k: s[k] for k in ("id", "version", "name", "approver_user_id", "status")}
                for s in steps
            ],
        }

    async def _preview_plan_review(
        self, who: FinancePrincipal, body: ActionPreview, key: str
    ) -> dict[str, Any]:
        """批准/驳回的计划只是本人决定意图，不再递归创建审批。"""
        request = body.model_dump(mode="json", exclude_none=True)
        async with self.pool.acquire() as conn, conn.transaction():
            original = await self._plan_row(conn, who, body.target_id)
            doc = await self._claim_row(conn, who, original["target_id"], lock=True)
            old = await self._business_previous(
                conn, who, doc["legal_entity_id"], "previewFinanceAction", key, request
            )
            if old is not None:
                return await self._plan_view(conn, who, await self._plan_row(conn, who, old["id"]))
            blockers = await self._check_plan_review(conn, who, original, body)
            workflow, steps = await self._plan_workflow(conn, original)
            bindings = {
                "original_digest": original["digest"],
                "workflow_version": workflow["version"] if workflow else None,
                "steps": [
                    {k: s[k] for k in ("id", "version", "status", "approver_user_id")}
                    for s in steps
                ],
            }
            payload = {
                "id": identifier(),
                "version": 1,
                "legal_entity_id": doc["legal_entity_id"],
                "action": body.action,
                "simulation": decode(original["data"]).get("simulation", False),
                "target_type": "action_plan",
                "target_id": original["id"],
                "target_version": 1,
                "parameters": request["parameters"],
                "expires_at": min(
                    original["expires_at"], datetime.now(UTC) + timedelta(minutes=15)
                ).isoformat(),
                "required_approvals": [],
                "effects": ["记录本人对固定原计划的实际审批决定"],
                "actor_id": who.user_id,
                "tenant_id": who.tenant_id,
                "bindings": bindings,
            }
            digest = request_digest(payload)
            await self._insert_settlement_plan(conn, who, doc, payload, digest)
            return await self._business_remember(
                conn,
                who,
                doc["legal_entity_id"],
                "previewFinanceAction",
                key,
                request,
                self._public_plan(payload, digest, blockers),
            )

    async def _check_plan_review(
        self,
        conn: Connection,
        who: FinancePrincipal,
        original: Any,
        body: ActionPreview,
        *,
        lock: bool = False,
    ) -> list[str]:
        """仅当前步骤实际人员决定，原计划失效或过期即拒绝审批。"""
        payload = decode(original["data"])
        if payload["action"] not in EXTERNAL_ACTIONS:
            return ["PLAN_NOT_REVIEWABLE:不能为批准/驳回计划再次批准"]
        if body.expected_version != 1:
            return ["VERSION_CONFLICT:原不可变计划版本为1"]
        blockers = await self._external_plan_blockers(conn, who, original, lock=lock, approval=True)
        if blockers:
            return blockers
        workflow, steps = await self._plan_workflow(conn, original, lock=lock)
        if workflow is None or workflow["status"] != "under_review":
            return ["WORKFLOW_NOT_ACTIVE:专用审批已结束"]
        doc = await self._claim_row(conn, who, original["target_id"])
        active = next((s for s in steps if s["status"] == "pending"), None)
        if (
            active is None
            or active["id"] != body.parameters.workflow_step_id
            or active["approver_user_id"] != who.user_id
            or who.user_id in {doc["owner_id"], original["owner_id"]}
        ):
            return ["STEP_FORBIDDEN:仅当前实际分派人可决定且禁止本人自审"]
        try:
            for step in steps:
                await self._current_approver(
                    conn, doc, step["approver_user_id"], step["authority_generation"]
                )
        except FinanceError as exc:
            return [f"{exc.code}:{exc.message}"]
        evidence = {e["id"] for e in await self._claim_evidence(conn, doc)}
        if not set(body.parameters.evidence_ids or []) <= evidence:
            return ["EVIDENCE_FORBIDDEN:证据不属于原单据"]
        return []

    async def _plan_review_blockers(
        self, conn: Connection, who: FinancePrincipal, row: Any, *, lock: bool = False
    ) -> list[str]:
        """派生人工决定绑定原摘要和当前步骤版本，防止重复或越步。"""
        payload = decode(row["data"])
        if row["expires_at"] <= datetime.now(UTC):
            return ["PLAN_EXPIRED:决定计划已过期"]
        original = await self._plan_row(conn, who, payload["target_id"])
        body = ActionPreview(
            action=payload["action"],
            target_type="action_plan",
            target_id=original["id"],
            expected_version=1,
            parameters=payload["parameters"],
        )
        blockers = await self._check_plan_review(conn, who, original, body, lock=lock)
        workflow, steps = await self._plan_workflow(conn, original, lock=lock)
        bindings = {
            "original_digest": original["digest"],
            "workflow_version": workflow["version"] if workflow else None,
            "steps": [
                {k: s[k] for k in ("id", "version", "status", "approver_user_id")} for s in steps
            ],
        }
        if bindings != payload["bindings"]:
            blockers.append("PLAN_INVALIDATED:原计划摘要或实际审核步骤已变化")
        return blockers

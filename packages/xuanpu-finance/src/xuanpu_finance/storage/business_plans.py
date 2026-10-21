"""十五分钟不可变行动计划与实时重新校验。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ActionPreview, ExpenseDraft
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.business_allocations import AllocationStore
from xuanpu_finance.storage.common import decode, identifier

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class PlanStore(AllocationStore):
    """计划只冻结意图和证据，业务状态必须由人工执行推进。"""

    async def preview_finance_action(
        self, who: FinancePrincipal, body: ActionPreview, key: str
    ) -> dict[str, Any]:
        """预检不占额度，成功和阻断计划均保留不可变正文。"""
        request = body.model_dump(mode="json", exclude_none=True)
        async with self.pool.acquire() as conn, conn.transaction():
            claim = await self._claim_row(conn, who, body.target_id, lock=True)
            entity = claim["legal_entity_id"]
            previous = await self._business_previous(
                conn, who, entity, "previewFinanceAction", key, request
            )
            if previous is not None:
                row = await self._plan_row(conn, who, previous["id"])
                return await self._plan_view(conn, who, row)
            bindings = await self._action_bindings(conn, who, claim, body)
            blockers, approvals = await self._action_check(conn, who, claim, body, lock=False)
            payload = {
                "id": identifier(),
                "version": 1,
                "legal_entity_id": entity,
                "action": body.action,
                "target_type": body.target_type,
                "target_id": body.target_id,
                "target_version": body.expected_version,
                "parameters": request["parameters"],
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "required_approvals": approvals,
                "effects": self._action_effects(body.action),
                "actor_id": who.user_id,
                "tenant_id": who.tenant_id,
                "bindings": bindings,
            }
            if bindings["workflow"] is not None:
                payload["workflow_id"] = bindings["workflow"]["id"]
            if claim["record_mode"] == "internal_record":
                payload["effects"].append("内部记录，未关联结构化发票时不代表付款、入账或验真")
            digest = request_digest(payload)
            await conn.execute(
                "INSERT INTO finance_action_plan VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)",
                who.tenant_id,
                entity,
                payload["id"],
                who.user_id,
                claim["id"],
                digest,
                datetime.fromisoformat(payload["expires_at"]),
                json.dumps(payload),
            )
            result = self._public_plan(payload, digest, blockers)
            return await self._business_remember(
                conn, who, entity, "previewFinanceAction", key, request, result
            )

    async def get_action_plan(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """只重算可执行性，不改正文、摘要、版本或到期时间。"""
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._plan_row(conn, who, id)
            return await self._plan_view(conn, who, row)

    async def _plan_row(self, conn: Connection, who: FinancePrincipal, id: str) -> Any:
        """计划仅由原actor读取和执行，不能转授。"""
        row = await conn.fetchrow(
            "SELECT * FROM finance_action_plan WHERE tenant_id=$1 AND id=$2", who.tenant_id, id
        )
        if row is None or row["owner_id"] != who.user_id:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "行动计划不存在")
        await self._role(conn, who, row["legal_entity_id"])
        return row

    def _public_plan(
        self, payload: dict[str, Any], digest: str, blockers: list[str]
    ) -> dict[str, Any]:
        """仅投影契约允许字段，内部绑定和身份不向客户端暴露。"""
        fields = {
            key: value
            for key, value in payload.items()
            if key not in {"actor_id", "tenant_id", "bindings"}
        }
        return {**fields, "digest": digest, "executable": not blockers, "blockers": blockers}

    def _action_effects(self, action: str) -> list[str]:
        """明确展示每种动作的业务影响，不声称付款或核销。"""
        return {
            "submit_claim": ["预留发票额度并开始人工审批"],
            "approve": ["记录本人决定并推进审批；最后一步通过后形成报销批准，额度仍预留"],
            "reject": ["驳回报销、取消余下审批并释放预留额度"],
            "withdraw": ["撤回本人报销、取消余下审批并释放预留额度"],
        }[action]

    async def _plan_view(self, conn: Connection, who: FinancePrincipal, row: Any) -> dict[str, Any]:
        """计划读取时再次核对当前人员、制度、版本和步骤。"""
        payload = decode(row["data"])
        claim = await self._claim_row(conn, who, row["target_id"])
        blockers = await self._plan_blockers(conn, who, row, claim, lock=False)
        return self._public_plan(payload, row["digest"], blockers)

    async def _plan_blockers(
        self, conn: Connection, who: FinancePrincipal, row: Any, claim: Any, *, lock: bool
    ) -> list[str]:
        """预检和执行使用相同校验，完整绑定变化或过期即拒绝。"""
        payload = decode(row["data"])
        body = ActionPreview(
            action=payload["action"],
            target_type=payload["target_type"],
            target_id=payload["target_id"],
            expected_version=payload["target_version"],
            parameters=payload["parameters"],
        )
        blockers, _ = await self._action_check(conn, who, claim, body, lock=lock)
        bindings = await self._action_bindings(conn, who, claim, body)
        if bindings != payload["bindings"]:
            blockers.append("PLAN_STALE:单据、分配、票据、制度或审批步骤已变化")
        if row["expires_at"] <= datetime.now(UTC):
            blockers.append("PLAN_EXPIRED:行动计划已过期")
        used = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_business_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND plan_id=$3)",
            who.tenant_id,
            row["legal_entity_id"],
            row["id"],
        )
        if used:
            blockers.append("PLAN_ALREADY_EXECUTED:计划已执行")
        return list(dict.fromkeys(blockers))

    async def _action_bindings(
        self, conn: Connection, who: FinancePrincipal, claim: Any, body: ActionPreview
    ) -> dict[str, Any]:
        """冻结完整分配、原始票快照、证据以及当前流程和步骤版本。"""
        policy = await self._policy(conn, who.tenant_id, claim["legal_entity_id"], require=False)
        allocations = await conn.fetch(
            "SELECT a.*,i.data AS invoice_data FROM finance_allocation a JOIN finance_invoice i ON(i.tenant_id=a.tenant_id AND i.legal_entity_id=a.legal_entity_id AND i.id=a.invoice_id) WHERE a.tenant_id=$1 AND a.legal_entity_id=$2 AND a.document_id=$3 ORDER BY a.invoice_id,a.id",
            who.tenant_id,
            claim["legal_entity_id"],
            claim["id"],
        )
        workflow, steps = await self._workflow_state(conn, claim)
        allocation_data = [
            {
                "id": row["id"],
                "version": row["version"],
                "invoice_id": row["invoice_id"],
                "invoice_version": row["invoice_version"],
                "invoice_line_id": row["invoice_line_id"],
                "amount": str(row["amount"]),
                "tax": str(row["tax"]),
                "state": row["state"],
                "invoice": decode(row["invoice_data"]),
            }
            for row in allocations
        ]
        flow = (
            None
            if workflow is None
            else {
                key: workflow[key]
                for key in (
                    "id",
                    "version",
                    "status",
                    "target_version",
                    "policy_version",
                    "policy_digest",
                )
            }
        )
        if flow is not None:
            flow["due_at"] = workflow["due_at"].isoformat()
        participants = {who.user_id, *(step["approver_user_id"] for step in steps)}
        if policy is not None:
            participants.update(step["approver_user_id"] for step in policy["steps"])
        return {
            "authorities": [
                dict(record)
                for record in await conn.fetch(
                    "SELECT user_id,role,generation FROM finance_grant_epoch WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=ANY($3::text[]) ORDER BY user_id",
                    who.tenant_id,
                    claim["legal_entity_id"],
                    sorted(participants),
                )
            ],
            "claim_version": claim["version"],
            "claim_status": claim["status"],
            "record_mode": claim["record_mode"],
            "total": str(claim["total"]),
            "owner_id": claim["owner_id"],
            "policy": None
            if policy is None
            else {
                "version": policy["version"],
                "digest": policy["digest"],
                "generation": policy["generation"],
            },
            "allocations": allocation_data,
            "evidence": await self._claim_evidence(conn, claim),
            "workflow": flow,
            "steps": [
                {key: step[key] for key in ("id", "version", "status", "approver_user_id")}
                for step in steps
            ],
            "parameters": body.parameters.model_dump(mode="json", exclude_none=True),
        }

    async def _workflow_state(
        self, conn: Connection, claim: Any, *, lock: bool = False
    ) -> tuple[Any, list[Any]]:
        """单据后锁当前工作流，再按固定顺序读取步骤。"""
        workflow = await conn.fetchrow(
            "SELECT * FROM finance_workflow WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 ORDER BY target_version DESC LIMIT 1"
            + (" FOR UPDATE" if lock else ""),
            claim["tenant_id"],
            claim["legal_entity_id"],
            claim["id"],
        )
        if workflow is None:
            return None, []
        steps = await conn.fetch(
            "SELECT * FROM finance_workflow_step WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 ORDER BY position"
            + (" FOR UPDATE" if lock else ""),
            claim["tenant_id"],
            claim["legal_entity_id"],
            workflow["id"],
        )
        return workflow, list(steps)

    async def _action_check(
        self,
        conn: Connection,
        who: FinancePrincipal,
        claim: Any,
        body: ActionPreview,
        *,
        lock: bool,
    ) -> tuple[list[str], list[str]]:
        """将可预期领域阻断投影为预检结果，执行仍将拒绝该计划。"""
        try:
            approvals = await self._validate_action(conn, who, claim, body, lock=lock)
            return [], approvals
        except FinanceError as exc:
            return [f"{exc.code}:{exc.message}"], []

    async def _validate_action(
        self,
        conn: Connection,
        who: FinancePrincipal,
        claim: Any,
        body: ActionPreview,
        *,
        lock: bool,
    ) -> list[str]:
        """校验本人提交/撤回、分派审批和不可绕过的额度硬约束。"""
        if claim["version"] != body.expected_version:
            raise FinanceError("VERSION_CONFLICT", 409, "单据版本已变化")
        if body.action == "withdraw":
            if claim["owner_id"] != who.user_id or claim["status"] not in {
                "under_review",
                "approved",
            }:
                raise FinanceError(
                    "WITHDRAW_NOT_ALLOWED", 403, "仅申请人可撤回待审或已批准未执行业务"
                )
            return []
        policy = await self._policy(conn, who.tenant_id, claim["legal_entity_id"])
        assert policy is not None
        if body.action == "submit_claim":
            self._owner_edit(who, claim, body.expected_version)
            self._validate_draft(who, ExpenseDraft(**decode(claim["content"])), policy)
            if claim["record_mode"] != policy["record_mode"]:
                raise FinanceError("RECORD_MODE_CHANGED", 409, "制度记录模式已变化，请先修订单据")
            await self._validate_receipts(
                conn,
                who,
                claim["legal_entity_id"],
                decode(claim["content"]).get("evidence_ids", []),
            )
            steps = await self._applicable_steps(conn, claim, policy)
            await self._reservation_check(conn, claim, policy, lock=lock)
            return [step["approver_user_id"] for step in steps]
        workflow, steps = await self._workflow_state(conn, claim, lock=lock)
        await self._validate_review(conn, who, claim, body, policy, workflow, steps)
        if body.action == "approve":
            if workflow["due_at"] <= datetime.now(UTC):
                raise FinanceError("WORKFLOW_OVERDUE", 409, "审批已逾期，请申请人核对后撤回重提")
            await self._reserved_check(conn, claim, policy, lock=lock)
        return []

    async def _applicable_steps(
        self, conn: Connection, claim: Any, policy: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """门槛按报销总额筛选；空链和申请人自审均不得自动批准。"""
        steps = [
            step for step in policy["steps"] if claim["total"] >= Decimal(step["minimum_amount"])
        ]
        if not steps or any(step["approver_user_id"] == claim["owner_id"] for step in steps):
            raise FinanceError("APPROVAL_CHAIN_INVALID", 409, "审批链为空或包含申请人本人")
        for step in steps:
            await self._current_approver(conn, claim, step["approver_user_id"])
        return steps

    async def _current_approver(
        self, conn: Connection, claim: Any, user_id: str, expected_generation: int | None = None
    ) -> int:
        """每条业务命令重查当前公司审批资格并锁住授权行。"""
        role = await conn.fetchval(
            "SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 FOR SHARE",
            claim["tenant_id"],
            claim["legal_entity_id"],
            user_id,
        )
        if role not in {"approver", "reviewer"}:
            raise FinanceError("APPROVER_REVOKED", 403, "原审批人公司资格已撤销")
        generation = await conn.fetchval(
            "SELECT generation FROM finance_grant_epoch WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 FOR SHARE",
            claim["tenant_id"],
            claim["legal_entity_id"],
            user_id,
        )
        if generation is None or (
            expected_generation is not None and generation != expected_generation
        ):
            raise FinanceError("APPROVER_REVOKED", 403, "审批授权已变更，请申请人撤回重提")
        return int(generation)

    async def _validate_review(
        self,
        conn: Connection,
        who: FinancePrincipal,
        claim: Any,
        body: ActionPreview,
        policy: dict[str, Any],
        workflow: Any,
        steps: list[Any],
    ) -> None:
        """公司reviewer读权不等于当前步骤审批权。"""
        if (
            claim["status"] != "under_review"
            or workflow is None
            or workflow["status"] != "under_review"
        ):
            raise FinanceError("WORKFLOW_NOT_ACTIVE", 409, "审批流程已结束或尚未提交")
        if (
            workflow["policy_version"] != policy["version"]
            or workflow["policy_digest"] != policy["digest"]
            or workflow["policy_generation"] != policy["generation"]
        ):
            raise FinanceError("POLICY_CHANGED", 409, "制度已变化，请申请人撤回后重新提交")
        if workflow["target_version"] != claim["version"]:
            raise FinanceError("VERSION_CONFLICT", 409, "审批目标版本已变化")
        for step in steps:
            await self._current_approver(
                conn, claim, step["approver_user_id"], step["authority_generation"]
            )
        active = next((step for step in steps if step["status"] == "pending"), None)
        if (
            active is None
            or active["id"] != body.parameters.workflow_step_id
            or active["approver_user_id"] != who.user_id
            or who.user_id == claim["owner_id"]
        ):
            raise FinanceError("STEP_FORBIDDEN", 403, "仅当前步骤实际分派者可决定，禁止自审")
        previous = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_decision WHERE tenant_id=$1 AND legal_entity_id=$2 AND workflow_id=$3 AND actor_id=$4)",
            who.tenant_id,
            claim["legal_entity_id"],
            workflow["id"],
            who.user_id,
        )
        if previous:
            raise FinanceError("DUPLICATE_REVIEWER", 409, "该人员已经作出决定")
        evidence = {item["id"] for item in await self._claim_evidence(conn, claim)}
        if not set(body.parameters.evidence_ids or []) <= evidence:
            raise FinanceError("EVIDENCE_FORBIDDEN", 403, "证据不存在或不属于该报销")

"""外部计划的业务批准、原件、渠道、账户与部分金额不变量。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import BusinessRef
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.storage.common import decode
from xuanpu_finance.storage.f2_documents import INFLIGHT, SettlementDocumentStore

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ActionPreview
    from xuanpu_finance.storage.business_base import Connection


class SettlementCheckStore(SettlementDocumentStore):
    """预检与发送共享资格检查，不用HTTP成功代替资金或凭证事实。"""

    async def _external_bindings(
        self,
        conn: Connection,
        who: FinancePrincipal,
        doc: Any,
        body: ActionPreview,
        *,
        lock: bool,
        exclude_operation_id: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """分别检查业务资格、渠道、金额与固定参数，再组合不可变绑定。"""
        policy, business_steps = await self._external_prerequisites(conn, who, doc, body, lock=lock)
        payment = body.action == "execute_payment"
        provider = await self._external_provider(conn, who, doc, policy, payment=payment)
        amount = Decimal(body.parameters.amount or "0")
        await self._settlement_balance(conn, doc, body, amount, exclude_operation_id)
        steps_key = (
            "payment_steps"
            if payment
            else "reversal_steps"
            if body.action == "reverse_posting"
            else "posting_steps"
        )
        steps = [s for s in policy[steps_key] if amount >= Decimal(s["minimum_amount"])]
        if not steps or any(s["approver_user_id"] in {doc["owner_id"], who.user_id} for s in steps):
            raise FinanceError(
                "APPROVAL_CHAIN_INVALID", 409, "专用审批链为空或包含申请人/经办人本人"
            )
        people = {
            who.user_id,
            doc["owner_id"],
            *(s["approver_user_id"] for s in business_steps),
            *(s["approver_user_id"] for s in steps),
        }
        for s in steps:
            await self._current_approver(conn, doc, s["approver_user_id"])
        bindings: dict[str, Any] = {
            "document_version": doc["version"],
            "document_kind": doc["document_kind"],
            "simulation": doc["simulation"],
            "business": await self._action_bindings(conn, who, doc, body),
            "settlement_policy": policy,
            "provider": {
                **provider,
            },
            "authorities": [
                dict(r)
                for r in await conn.fetch(
                    "SELECT user_id,role,generation FROM finance_grant_epoch WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=ANY($3::text[]) ORDER BY user_id",
                    who.tenant_id,
                    doc["legal_entity_id"],
                    sorted(people),
                )
            ],
        }
        bindings.update(await self._external_parameter_bindings(conn, who, doc, body, policy))
        if doc["document_kind"] == "payable":
            content = decode(doc["content"])
            owner = FinancePrincipal(doc["tenant_id"], doc["owner_id"])
            bindings["business_documents"] = [
                await self._catalog_ref(
                    conn, owner, doc["legal_entity_id"], BusinessRef.model_validate(ref)
                )
                for ref in [content["supplier_ref"], *content["business_refs"]]
            ]
        return bindings, steps

    async def _external_prerequisites(
        self, conn: Connection, who: FinancePrincipal, doc: Any, body: ActionPreview, *, lock: bool
    ) -> tuple[dict[str, Any], list[Any]]:
        """外部资格独立复核原业务批准、有效角色和实际原件。"""
        if body.target_type != "posting" and (
            body.target_type != doc["document_kind"] or body.expected_version != doc["version"]
        ):
            raise FinanceError("VERSION_CONFLICT", 409, "单据类型或版本已变化")
        if doc["record_mode"] != "invoice_backed":
            raise FinanceError(
                "INTERNAL_RECORD_NOT_EXECUTABLE", 409, "内部记录永远不能付款或正式入账"
            )
        if doc["simulation"] != await self._simulation(conn):
            raise FinanceError("SIMULATION_MODE_CONFLICT", 409, "历史单据与实例环境不一致")
        if doc["status"] != "approved":
            raise FinanceError("BUSINESS_APPROVAL_REQUIRED", 409, "原单据尚未取得业务批准")
        policy = await self._settlement_policy(conn, who.tenant_id, doc["legal_entity_id"])
        assert policy is not None
        payment = body.action == "execute_payment"
        executors = policy["payment_executor_user_ids" if payment else "posting_executor_user_ids"]
        if (
            who.user_id == doc["owner_id"]
            or who.user_id not in executors
            or await self._role(conn, who, doc["legal_entity_id"]) != "reviewer"
        ):
            raise FinanceError("EXECUTOR_FORBIDDEN", 403, "必须由与申请人分离的当前专用经办人执行")
        business_policy = await self._policy(conn, who.tenant_id, doc["legal_entity_id"])
        assert business_policy is not None
        workflow, business_steps = await self._workflow_state(conn, doc, lock=lock)
        grant = await conn.fetchrow(
            "SELECT * FROM finance_execution_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=$3 AND scope='expense_approved' AND target_version=$4 AND active",
            who.tenant_id,
            doc["legal_entity_id"],
            doc["id"],
            doc["version"],
        )
        if workflow is None or workflow["status"] != "approved" or grant is None:
            raise FinanceError("BUSINESS_APPROVAL_REQUIRED", 409, "原业务批准授权无效")
        if (grant["policy_version"], grant["policy_digest"], grant["policy_generation"]) != (
            business_policy["version"],
            business_policy["digest"],
            business_policy["generation"],
        ):
            raise FinanceError("POLICY_CHANGED", 409, "原业务制度已变化")
        for step in business_steps:
            await self._current_approver(
                conn, doc, step["approver_user_id"], step["authority_generation"]
            )
        await self._reserved_check(conn, doc, business_policy, lock=lock)
        await self._external_invoice_evidence(conn, doc, policy)
        return policy, business_steps

    async def _external_provider(
        self,
        conn: Connection,
        who: FinancePrincipal,
        doc: Any,
        policy: dict[str, Any],
        *,
        payment: bool,
    ) -> dict[str, Any]:
        """新发送必须使用当前启用且支持幂等和查单的固定渠道。"""
        prefix = "payment" if payment else "posting"
        provider = await conn.fetchrow(
            "SELECT * FROM finance_settlement_provider WHERE tenant_id=$1 AND legal_entity_id=$2 AND provider=$3 AND configuration_version=$4 FOR SHARE",
            who.tenant_id,
            doc["legal_entity_id"],
            policy[prefix + "_provider"],
            policy[prefix + "_configuration_version"],
        )
        if provider is None or not provider["enabled"]:
            raise FinanceError("PROVIDER_UNAVAILABLE", 409, "渠道未配置或已停用")
        provider_data = decode(provider["data"])
        if (
            provider_data["simulation"] != doc["simulation"]
            or not provider_data["supports_idempotency"]
            or not provider_data["supports_query"]
        ):
            raise FinanceError(
                "PROVIDER_CAPABILITY_REQUIRED", 409, "渠道环境、幂等或查单能力不满足"
            )
        return {**provider_data, "generation": provider["generation"], "digest": provider["digest"]}

    async def _external_parameter_bindings(
        self,
        conn: Connection,
        who: FinancePrincipal,
        doc: Any,
        body: ActionPreview,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        """只从受控目录填账户和账套，并绑定原凭证与证据范围。"""
        payment = body.action == "execute_payment"
        bindings: dict[str, Any] = {}
        if payment:
            assert body.parameters.payee_ref is not None
            account = await self._catalog_ref(
                conn, who, doc["legal_entity_id"], body.parameters.payee_ref
            )
            subject = (
                doc["owner_id"]
                if doc["document_kind"] == "expense_claim"
                else decode(doc["content"])["supplier_ref"]["id"]
            )
            kind = (
                "employee_account"
                if doc["document_kind"] == "expense_claim"
                else "supplier_account"
            )
            if (
                not account["verified"]
                or account["subject_id"] != subject
                or account["ref"]["kind"] != kind
            ):
                raise FinanceError("PAYEE_MISMATCH", 409, "收款账户与单据实际员工或供应商不一致")
            bindings["payee"] = account
        else:
            if body.parameters.accounting_period not in policy["open_periods"]:
                raise FinanceError("ACCOUNTING_PERIOD_CLOSED", 409, "期间未开放，须走财务调整")
            bindings.update(
                {
                    "ledger_id": policy["ledger_id"],
                    "accounting_period": body.parameters.accounting_period,
                }
            )
        if body.action == "reverse_posting":
            original = await conn.fetchrow(
                "SELECT * FROM finance_posting WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
                who.tenant_id,
                doc["legal_entity_id"],
                body.target_id,
            )
            assert original is not None
            bindings["original_posting"] = {
                "id": original["id"],
                "version": original["version"],
                "amount": str(original["amount"]),
                "data": decode(original["data"]),
            }
        if body.parameters.evidence_ids:
            evidence = {e["id"] for e in await self._claim_evidence(conn, doc)}
            if not set(body.parameters.evidence_ids) <= evidence:
                raise FinanceError("EVIDENCE_FORBIDDEN", 403, "证据不属于原单据")
        return bindings

    async def _external_invoice_evidence(
        self, conn: Connection, doc: Any, policy: dict[str, Any]
    ) -> None:
        """正式外部执行要求有效查验和原件，不把passed字符串当充分事实。"""
        rows = await conn.fetch(
            "SELECT i.data,a.invoice_id FROM finance_allocation a JOIN finance_invoice i ON(i.tenant_id=a.tenant_id AND i.legal_entity_id=a.legal_entity_id AND i.id=a.invoice_id) WHERE a.tenant_id=$1 AND a.legal_entity_id=$2 AND a.document_id=$3 AND a.state IN ('reserved','consumed')",
            doc["tenant_id"],
            doc["legal_entity_id"],
            doc["id"],
        )
        if not rows:
            raise FinanceError("INVOICE_EVIDENCE_REQUIRED", 409, "外部执行须有完整原件及结构化分配")
        if doc["simulation"] and not policy["require_verified_invoice"]:
            return
        for row in rows:
            data = decode(row["data"])
            evidence = data.get("verification_evidence")
            if data.get("verification_status") != "passed" or not isinstance(evidence, dict):
                raise FinanceError(
                    "VALID_VERIFICATION_REQUIRED", 409, "正式执行须有当前有效查验渠道证据"
                )
            try:
                valid_until = datetime.fromisoformat(evidence["valid_until"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FinanceError("VALID_VERIFICATION_REQUIRED", 409, "查验证据期限无效") from exc
            originals = await conn.fetchval(
                "SELECT count(*) FROM finance_invoice_receipt l JOIN finance_receipt r ON(r.tenant_id=l.tenant_id AND r.legal_entity_id=l.legal_entity_id AND r.id=l.receipt_id) WHERE l.tenant_id=$1 AND l.legal_entity_id=$2 AND l.invoice_id=$3 AND r.sha256<>''",
                doc["tenant_id"],
                doc["legal_entity_id"],
                row["invoice_id"],
            )
            if (
                valid_until.tzinfo is None
                or valid_until <= datetime.now(UTC)
                or not evidence.get("provider")
                or not evidence.get("raw_response_ref")
                or not originals
            ):
                raise FinanceError(
                    "VALID_VERIFICATION_REQUIRED", 409, "正式查验或原件证据缺失/过期"
                )

    async def _settlement_balance(
        self,
        conn: Connection,
        doc: Any,
        body: ActionPreview,
        amount: Decimal,
        exclude_operation_id: str | None = None,
    ) -> None:
        """父单据锁下复核部分付款/记账/冲回的所有成功和在途金额。"""
        totals = await self._settlement_totals(conn, doc, exclude_operation_id)
        if body.action == "reverse_posting":
            original = await conn.fetchrow(
                "SELECT * FROM finance_posting WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
                doc["tenant_id"],
                doc["legal_entity_id"],
                body.target_id,
            )
            if (
                original is None
                or original["status"] != "succeeded"
                or original["original_posting_id"] is not None
            ):
                raise FinanceError("POSTING_NOT_REVERSIBLE", 409, "仅成功原始入账可冲回")
            if original["version"] != body.expected_version:
                raise FinanceError("VERSION_CONFLICT", 409, "原凭证版本已变化")
            used = await conn.fetchval(
                "SELECT COALESCE(sum(amount),0) FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND target_id=$3 AND action='reverse_posting' AND status=ANY($4::text[]) AND ($5::text IS NULL OR id<>$5)",
                doc["tenant_id"],
                doc["legal_entity_id"],
                body.target_id,
                [*INFLIGHT, "succeeded"],
                exclude_operation_id,
            )
            available = original["amount"] - used
        elif body.action == "execute_payment":
            available = (
                doc["total"]
                - Decimal(totals["paid_amount"])
                - Decimal(totals["payment_pending_amount"])
            )
        else:
            available = (
                doc["total"]
                - Decimal(totals["posted_amount"])
                - Decimal(totals["posting_pending_amount"])
            )
        if amount > available:
            raise FinanceError(
                "SETTLEMENT_BALANCE_EXCEEDED", 409, "本次金额超过扣除成功及在途后的可用余额"
            )

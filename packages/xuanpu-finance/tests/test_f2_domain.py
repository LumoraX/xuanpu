"""独立真实PG上的应付与外部执行金额不变量。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ActionPreview, PayableDraft
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.settlement.models import BusinessSnapshot, PayeeAccount, SettlementPolicy

from .test_expense_domain import execute, invoice, setup


def ref(kind: str, id: str) -> dict[str, str]:
    """构造带完整来源和摘要的固定测试引用。"""
    return {"kind": kind, "source": "controlled", "id": id, "version": "v1", "sha256": "a" * 64}


async def configured(
    store: Any,
    *,
    simulation: bool = True,
) -> tuple[FinancePrincipal, FinancePrincipal, FinancePrincipal, FinancePrincipal]:
    """显式配置隔离模拟环境与不同的资金、记账和审核人员。"""
    assert hasattr(store, "configure_settlements"), "缺少F2受控配置领域接口"
    owner, approvers = await setup(store)
    payer = FinancePrincipal("t", "payer", origin="human")
    accountant = FinancePrincipal("t", "accountant", origin="human")
    for user in (payer, accountant):
        await store.grant("t", user.user_id, "e", "reviewer")
    policy = SettlementPolicy(
        tenant_id="t",
        legal_entity_id="e",
        version=1,
        enabled=True,
        payment_provider="test",
        payment_configuration_version=1,
        posting_provider="test",
        posting_configuration_version=1,
        ledger_id="ledger",
        open_periods=["2026-10"],
        payment_executor_user_ids=["payer"],
        posting_executor_user_ids=["accountant"],
        payment_steps=[{"id": "pay", "name": "付款审核", "approver_user_id": "a"}],
        posting_steps=[{"id": "post", "name": "记账审核", "approver_user_id": "a"}],
        reversal_steps=[{"id": "rev", "name": "冲回审核", "approver_user_id": "a"}],
        require_verified_invoice=not simulation,
    )
    documents = [
        BusinessSnapshot(
            tenant_id="t",
            legal_entity_id="e",
            ref=ref(kind, id),
            name=id,
            verification_state="verified",
            allowed_user_ids=["owner"],
        )
        for kind, id in (("supplier", "supplier"), ("contract", "contract"))
    ]
    account = PayeeAccount(
        tenant_id="t",
        legal_entity_id="e",
        ref=ref("supplier_account", "account"),
        subject_id="supplier",
        account_name="供应商",
        masked_account="****0001",
        provider_account_id="provider-account",
        verified=True,
        allowed_user_ids=["payer", "owner"],
    )
    providers = [
        {
            "tenant_id": "t",
            "legal_entity_id": "e",
            "provider": "test",
            "configuration_version": 1,
            "adapter_version": "http-v1",
            "base_url": "http://127.0.0.1:1",
            "simulation": simulation,
            "enabled": True,
            "supports_idempotency": True,
            "supports_query": True,
        }
    ]
    await store.configure_settlements(
        [policy], documents, [account], providers, simulation=simulation
    )
    return owner, payer, accountant, approvers[0]


async def payable(store: Any, owner: FinancePrincipal, amount: str = "1000.00") -> dict[str, Any]:
    """原生应付绑定已取得的供应商与合同版本。"""
    return await store.create_payable(
        owner,
        PayableDraft(
            legal_entity_id="e",
            supplier_ref=ref("supplier", "supplier"),
            amount=amount,
            due_date="2026-11-01",
            business_refs=[ref("contract", "contract")],
            reason="采购",
        ),
        "payable",
    )


async def approved_payable(
    store: Any, *, simulation: bool = True
) -> tuple[Any, Any, Any, Any, dict[str, Any]]:
    """应付复用F1分票与实际人工顺序业务批准。"""
    owner, payer, accountant, approver = await configured(store, simulation=simulation)
    await invoice(store)
    doc = await payable(store, owner)
    from xuanpu_finance.expenses.models import AllocationRequest

    await store.create_invoice_allocation(
        owner,
        AllocationRequest(
            invoice_id="i",
            invoice_version=1,
            document_id=doc["id"],
            document_kind="payable",
            expected_document_version=doc["version"],
            amount="1000.00",
            tax="0.00",
        ),
        "allocation",
    )
    doc = await store.get_payable(owner, doc["id"])
    submit = await store.preview_finance_action(
        owner,
        ActionPreview(
            action="submit_payable",
            target_type="payable",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={},
        ),
        "submit",
    )
    assert submit["executable"], submit["blockers"]
    await execute(store, owner, submit)
    doc = await store.get_payable(owner, doc["id"])
    queue = await store.list_review_queue(approver, "e")
    approve = await store.preview_finance_action(
        approver,
        ActionPreview(
            action="approve",
            target_type="payable",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={"workflow_step_id": queue["items"][0]["workflow_step_id"]},
        ),
        "approve",
    )
    await execute(store, approver, approve)
    return owner, payer, accountant, approver, await store.get_payable(owner, doc["id"])


async def action_plan(
    store: Any,
    who: Any,
    approver: Any,
    doc: Any,
    action: str,
    amount: str = "600.00",
    key_suffix: str = "",
    **parameters: Any,
) -> dict[str, Any]:
    """专用审核针对同一不可变原计划，不借用单据批准。"""
    parameters = {"amount": amount, **parameters}
    if action == "execute_payment":
        parameters["payee_ref"] = ref("supplier_account", "account")
    else:
        parameters["accounting_period"] = "2026-10"
    plan = await store.preview_finance_action(
        who,
        ActionPreview(
            action=action,
            target_type=doc.get("kind", "payable"),
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters=parameters,
        ),
        action + amount + doc["id"] + key_suffix,
    )
    assert not plan["executable"]
    queue = await store.list_review_queue(approver, "e")
    item = next(item for item in queue["items"] if item["target_id"] == plan["id"])
    decision = await store.preview_finance_action(
        approver,
        ActionPreview(
            action="approve",
            target_type="action_plan",
            target_id=plan["id"],
            expected_version=1,
            parameters={"workflow_step_id": item["workflow_step_id"]},
        ),
        "approve-" + plan["id"],
    )
    assert decision["executable"], decision["blockers"]
    await execute(store, approver, decision)
    ready = await store.get_action_plan(who, plan["id"])
    assert ready["digest"] == plan["digest"]
    assert ready["executable"], ready["blockers"]
    return ready


async def test_payable_shared_approval_and_old_expense_api_isolation(store: Any) -> None:
    """原生应付完成业务批准，旧报销API不能读取或改写它。"""
    owner, _, _, _, doc = await approved_payable(store)
    assert doc["status"] == "approved" and doc["kind"] == "payable" and doc["simulation"]
    assert (await store.list_expense_claims(owner, "e"))["total"] == 0
    assert (await store.list_payables(owner, "e"))["total"] == 1
    with pytest.raises(FinanceError, match="RESOURCE_NOT_FOUND"):
        await store.get_expense_claim(owner, doc["id"])


async def test_specialized_plan_requires_human_and_reserves_partial_independent_amount(
    store: Any,
) -> None:
    """两种维度各自占额，未dispatch取消后才释放其金额。"""
    _, payer, accountant, approver, doc = await approved_payable(store)
    payment = await action_plan(store, payer, approver, doc, "execute_payment")
    with pytest.raises(FinanceError, match="HUMAN_REQUIRED"):
        await execute(store, replace(payer, origin="agent"), payment)
    op = await execute(store, payer, payment)
    assert op["status"] == "accepted" and op["id"].startswith("f2op_")
    assert (await execute(store, payer, payment))["id"] == op["id"]
    summary = await store.get_payable(payer, doc["id"])
    assert summary["payment_pending_amount"] == "600.00" and summary["posted_amount"] == "0.00"
    posting = await action_plan(
        store, accountant, approver, doc, "submit_posting", amount="1000.00"
    )
    assert (await execute(store, accountant, posting))["status"] == "accepted"
    bad = await store.preview_finance_action(
        payer,
        ActionPreview(
            action="execute_payment",
            target_type="payable",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={"amount": "600.00", "payee_ref": ref("supplier_account", "account")},
        ),
        "over",
    )
    assert any("BALANCE" in blocker for blocker in bad["blockers"])


def provider_result(request: dict[str, Any], status: str = "succeeded") -> dict[str, Any]:
    """回显固定请求归属并提供明确渠道资金或凭证证据。"""
    fields = {
        "protocol_version",
        "tenant_id",
        "legal_entity_id",
        "operation_id",
        "action",
        "external_request_id",
        "provider",
        "adapter_version",
        "configuration_version",
        "payload_hash",
        "simulation",
        "amount",
        "currency",
        "target",
        "plan_digest",
    }
    result = {k: v for k, v in request.items() if k in fields}
    result.update(
        {
            "status": status,
            "evidence": [
                {
                    "id": "proof",
                    "sha256": "b" * 64,
                    "media_type": "application/pdf",
                    "object_ref": "provider:proof",
                }
            ],
        }
    )
    if request["action"] == "execute_payment":
        result.update(
            {
                "external_order_id": "order-" + request["operation_id"],
                "transaction_id": "tx-" + request["operation_id"],
            }
        )
    else:
        result.update(
            {
                "ledger_id": request["ledger_id"],
                "accounting_period": request["accounting_period"],
                "voucher_id": "voucher-" + request["operation_id"],
            }
        )
    return result


async def test_worker_unknown_occupies_amount_and_restart_only_queries(store: Any) -> None:
    """发送后未知不重发，旧租约不能确认结果，查单成功才变已付。"""
    assert hasattr(store, "claim_settlement"), "缺少独立外部队列与租约事务接口"
    _, payer, _, approver, doc = await approved_payable(store)
    plan = await action_plan(store, payer, approver, doc, "execute_payment")
    op = await execute(store, payer, plan)
    job = await store.claim_settlement()
    assert job["mode"] == "submit" and set(job["authorization_user_ids"]) == {"payer", "a"}
    request = await store.prepare_settlement_dispatch(job)
    assert request["amount"] == "600.00" and request["simulation"]
    await store.defer_settlement(job, "NETWORK_TIMEOUT", uncertain=True)
    summary = await store.get_payable(payer, doc["id"])
    assert (
        summary["payment_status"] == "outcome_unknown"
        and summary["payment_pending_amount"] == "600.00"
    )
    await store.release_settlement(job)
    await store.pool.execute(
        "UPDATE finance_settlement_operation SET next_query_at=now()-interval '1 second' WHERE id=$1",
        op["id"],
    )
    recovered = await store.claim_settlement(allow_dispatch=False)
    assert recovered["mode"] == "query" and recovered["request"] == request
    with pytest.raises(FinanceError, match="LEASE_LOST"):
        await store.record_settlement_result(job, provider_result(request), b"old")
    await store.record_settlement_result(recovered, provider_result(request), b"success-proof")
    final = await store.get_payable(payer, doc["id"])
    assert (
        final["paid_amount"] == "600.00"
        and final["payment_pending_amount"] == "0.00"
        and final["posting_status"] == "not_posted"
    )
    assert (await store.list_payments(payer, "e"))["items"][0]["transaction_id"].startswith("tx-")
    assert await store.pool.fetchval("SELECT count(*) FROM finance_settlement_receipt") == 2
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_settlement_receipt WHERE data->>'stale'='true'"
        )
        == 1
    )


async def test_cancel_before_dispatch_releases_amount_but_withdraw_after_success_does_not(
    store: Any,
) -> None:
    """取消和发送竞态同锁串行，渠道成功永远不被撤回释放。"""
    assert hasattr(store, "claim_settlement"), "缺少独立外部队列与租约事务接口"
    owner, payer, _, approver, doc = await approved_payable(store)
    first = await action_plan(store, payer, approver, doc, "execute_payment")
    operation = await execute(store, payer, first)
    cancelled = await store.cancel_operation(payer, operation["id"], operation["version"], "cancel")
    assert cancelled["status"] == "cancelled"
    assert (await store.get_payable(payer, doc["id"]))["payment_pending_amount"] == "0.00"
    plan = await store.preview_finance_action(
        payer,
        ActionPreview(
            action="execute_payment",
            target_type="payable",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={"amount": "1000.00", "payee_ref": ref("supplier_account", "account")},
        ),
        "after-cancel",
    )
    queue = await store.list_review_queue(approver, "e")
    item = next(item for item in queue["items"] if item["target_id"] == plan["id"])
    review = await store.preview_finance_action(
        approver,
        ActionPreview(
            action="approve",
            target_type="action_plan",
            target_id=plan["id"],
            expected_version=1,
            parameters={"workflow_step_id": item["workflow_step_id"]},
        ),
        "review-after-cancel",
    )
    await execute(store, approver, review)
    await execute(store, payer, plan)
    job = await store.claim_settlement()
    request = await store.prepare_settlement_dispatch(job)
    await store.record_settlement_result(job, provider_result(request), b"receipt")
    withdraw = await store.preview_finance_action(
        owner,
        ActionPreview(
            action="withdraw",
            target_type="payable",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={"reason": "撤回"},
        ),
        "withdraw",
    )
    assert not withdraw["executable"] and any("EXTERNAL_EFFECT" in b for b in withdraw["blockers"])
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_allocation WHERE state IN ('reserved','consumed')"
        )
        == 1
    )


async def test_posting_reversal_has_independent_partial_amount_and_available_balance(
    store: Any,
) -> None:
    """先记账后付款允许，部分冲回保留原凭证并独立展示额度。"""
    _, _, accountant, approver, doc = await approved_payable(store)
    posting_plan = await action_plan(store, accountant, approver, doc, "submit_posting", "1000.00")
    await execute(store, accountant, posting_plan)
    job = await store.claim_settlement()
    request = await store.prepare_settlement_dispatch(job)
    await store.record_settlement_result(job, provider_result(request), b"posting")
    postings = await store.list_postings(accountant, "e")
    original = postings["items"][0]
    assert original["reversal_available_amount"] == "1000.00"
    reverse_plan = await action_plan(
        store,
        accountant,
        approver,
        {**original, "kind": "posting"},
        "reverse_posting",
        "400.00",
        reason="部分调整",
    )
    await execute(store, accountant, reverse_plan)
    assert (await store.get_posting(accountant, original["id"]))[
        "reversal_pending_amount"
    ] == "400.00"
    job = await store.claim_settlement()
    request = await store.prepare_settlement_dispatch(job)
    assert request["original_voucher_id"] == original["voucher_id"]
    await store.record_settlement_result(job, provider_result(request), b"reversal")
    summary = await store.get_payable(accountant, doc["id"])
    assert (
        summary["posted_amount"] == "600.00"
        and summary["reversed_amount"] == "400.00"
        and summary["paid_amount"] == "0.00"
    )
    assert (await store.get_posting(accountant, original["id"]))[
        "reversal_available_amount"
    ] == "600.00"


async def test_callback_only_schedules_query_and_same_event_different_bytes_is_conflict(
    store: Any,
) -> None:
    """验签后入站仍不直接改状态，同事件异文保全为异常。"""
    assert hasattr(store, "accept_provider_event"), "缺少验签后持久Inbox与查单调度接口"
    import hashlib
    import json

    _, payer, _, approver, doc = await approved_payable(store)
    plan = await action_plan(store, payer, approver, doc, "execute_payment")
    await execute(store, payer, plan)
    job = await store.claim_settlement()
    request = await store.prepare_settlement_dispatch(job)
    raw = json.dumps(provider_result(request)).encode()
    result = await store.accept_provider_event(
        "test", 1, "event", hashlib.sha256(raw).hexdigest(), raw
    )
    assert result["status"] == "accepted"
    assert (await store.get_payable(payer, doc["id"]))["paid_amount"] == "0.00"
    duplicate = await store.accept_provider_event(
        "test", 1, "event", hashlib.sha256(raw).hexdigest(), raw
    )
    assert duplicate["status"] == "duplicate"
    conflict = await store.accept_provider_event(
        "test", 1, "event", hashlib.sha256(b"other").hexdigest(), b"other"
    )
    assert conflict["status"] == "conflict"
    assert (await store.list_exceptions(payer, "e"))["total"] >= 1

"""F2配置撤销、隔离和并发执行的真实PG安全边界。"""

from __future__ import annotations

from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError

from .test_expense_domain import execute
from .test_f2_domain import action_plan, approved_payable


async def test_business_reference_revoke_restore_permanently_invalidates_old_plan(
    store: Any,
) -> None:
    """供应商/合同目录撤销恢复不能复活原专用计划或旧批准。"""
    _, payer, _, approver, doc = await approved_payable(store)
    plan = await action_plan(store, payer, approver, doc, "execute_payment")
    await store.pool.execute(
        "UPDATE finance_settlement_catalog SET enabled=false,generation=generation+1 WHERE kind='contract'"
    )
    blocked = await store.get_action_plan(payer, plan["id"])
    assert not blocked["executable"], "采购依据已撤销，原批准必须失效"
    await store.pool.execute(
        "UPDATE finance_settlement_catalog SET enabled=true,generation=generation+1 WHERE kind='contract'"
    )
    assert not (await store.get_action_plan(payer, plan["id"]))["executable"]
    with pytest.raises(FinanceError):
        await execute(store, payer, plan)


async def test_withdraw_cancels_unsent_external_records_atomically(store: Any) -> None:
    """单据撤回自动取消未发资金任务时，记录、版本、事件与Outbox一致。"""
    from xuanpu_finance.expenses.models import ActionPreview

    owner, payer, _, approver, doc = await approved_payable(store)
    plan = await action_plan(store, payer, approver, doc, "execute_payment")
    op = await execute(store, payer, plan)
    payment = (await store.list_payments(payer, "e"))["items"][0]
    withdraw = await store.preview_finance_action(
        owner,
        ActionPreview(
            action="withdraw",
            target_type="payable",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={"reason": "未发送前撤回"},
        ),
        "withdraw-unsent",
    )
    await execute(store, owner, withdraw)
    assert (await store.get_operation(payer, op["id"]))["status"] == "cancelled"
    after = await store.get_payment(payer, payment["id"])
    assert after["status"] == "cancelled" and after["version"] > payment["version"]
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_event WHERE kind='settlement.cancelled_by_withdraw'"
        )
        == 1
    )
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_outbox WHERE operation_id=$1", op["id"]
        )
        == 2
    )


async def test_two_approved_600_plans_cannot_overpay_1000_concurrently(store: Any) -> None:
    """两个已获专用批准的计划仍需在受理事务串行占额。"""
    import asyncio

    _, payer, _, approver, doc = await approved_payable(store)
    plans = [
        await action_plan(store, payer, approver, doc, "execute_payment", key_suffix=key)
        for key in ("one", "two")
    ]
    outcomes = await asyncio.gather(
        *(execute(store, payer, plan) for plan in plans), return_exceptions=True
    )
    assert sum(isinstance(r, dict) for r in outcomes) == 1
    assert sum(isinstance(r, FinanceError) for r in outcomes) == 1
    assert (await store.get_payable(payer, doc["id"]))["payment_pending_amount"] == "600.00"
    assert await store.pool.fetchval("SELECT count(*) FROM finance_settlement_operation") == 1


@pytest.mark.parametrize("person", ["payer", "a"])
async def test_executor_or_approver_revoke_restore_cannot_resurrect_plan(
    store: Any, person: str
) -> None:
    """经办或已批准人员撤权再恢复，旧固定摘要仍永久失效。"""
    _, payer, _, approver, doc = await approved_payable(store)
    plan = await action_plan(store, payer, approver, doc, "execute_payment")
    await store.grant("t", person, "e", "collector")
    await store.grant("t", person, "e", "reviewer" if person == "payer" else "approver")
    assert not (await store.get_action_plan(payer, plan["id"]))["executable"]
    with pytest.raises(FinanceError):
        await execute(store, payer, plan)


async def test_before_dispatch_rechecks_revoked_actor_and_releases_only_confirmed_unsent(
    store: Any,
) -> None:
    """已受理后发送前撤销经办资格，worker禁止固定请求且安全解除占额。"""
    _, payer, _, approver, doc = await approved_payable(store)
    plan = await action_plan(store, payer, approver, doc, "execute_payment")
    await execute(store, payer, plan)
    job = await store.claim_settlement()
    await store.grant("t", "payer", "e", "collector")
    with pytest.raises(FinanceError):
        await store.prepare_settlement_dispatch(job)
    await store.reject_settlement_before_dispatch(job, "ACTOR_REVOKED")
    await store.grant("t", "payer", "e", "reviewer")
    assert (await store.get_payable(payer, doc["id"]))["payment_pending_amount"] == "0.00"
    assert await store.pool.fetchval("SELECT request IS NULL FROM finance_settlement_operation")


async def test_database_simulation_mode_is_immutable_and_survives_store_rebuild(store: Any) -> None:
    """独立模拟库不能切换正式，同池新Store读取持久环境。"""
    from xuanpu_finance.storage import FinanceStore

    from .test_expense_domain import claim

    owner, _, _, _, _ = await approved_payable(store)
    rebuilt = FinanceStore(store.pool)
    new = await claim(rebuilt, owner, "1.00", key="rebuilt")
    assert new["simulation"]
    with pytest.raises(FinanceError, match="SIMULATION_MODE_CONFLICT"):
        await rebuilt.configure_settlements([], [], [], [], simulation=False)


async def test_result_event_failure_rolls_back_receipt_and_business_fact(store: Any) -> None:
    """回执/领域事件提交失败整批回滚，不留下已付或半份凭证。"""
    import asyncpg

    from .test_f2_domain import provider_result

    _, payer, _, approver, doc = await approved_payable(store)
    plan = await action_plan(store, payer, approver, doc, "execute_payment")
    await execute(store, payer, plan)
    job = await store.claim_settlement()
    request = await store.prepare_settlement_dispatch(job)
    await store.pool.execute(
        "CREATE FUNCTION reject_settlement_success() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.kind='settlement.succeeded' THEN RAISE EXCEPTION 'injected event failure'; END IF; RETURN NEW; END $$"
    )
    await store.pool.execute(
        "CREATE TRIGGER fail_result_event BEFORE INSERT ON finance_event FOR EACH ROW EXECUTE FUNCTION reject_settlement_success()"
    )
    with pytest.raises(asyncpg.RaiseError):
        await store.record_settlement_result(job, provider_result(request), b"must-rollback")
    assert await store.pool.fetchval("SELECT count(*) FROM finance_settlement_receipt") == 0
    assert (
        await store.pool.fetchval("SELECT status FROM finance_settlement_operation")
        == "dispatching"
    )
    assert (await store.get_payable(payer, doc["id"]))["paid_amount"] == "0.00"


async def test_former_approver_cannot_list_historical_other_users_records(store: Any) -> None:
    """审批人降为collector后，list/get及total必须一致隐藏他人单据和计划。"""
    _, payer, _, approver, doc = await approved_payable(store)
    plan = await action_plan(store, payer, approver, doc, "execute_payment")
    await execute(store, payer, plan)
    await store.grant("t", "a", "e", "collector")
    with pytest.raises(FinanceError, match="RESOURCE_NOT_FOUND"):
        await store.get_payable(approver, doc["id"])
    for method in (
        store.list_payables,
        store.list_payments,
        store.list_postings,
        store.list_action_plans,
    ):
        listing = await method(approver, "e")
        assert listing["total"] == 0 and listing["items"] == []


async def test_formal_passed_string_without_channel_original_evidence_is_blocked(
    store: Any,
) -> None:
    """F1关闭查验不能授予正式执行资格，单个passed字符串仍不足。"""
    from xuanpu_finance.expenses.models import ActionPreview

    from .test_f2_domain import ref

    _, payer, _, _, doc = await approved_payable(store, simulation=False)
    await store.pool.execute(
        "UPDATE finance_invoice SET data=jsonb_set(data,'{verification_status}','\"passed\"')"
    )
    plan = await store.preview_finance_action(
        payer,
        ActionPreview(
            action="execute_payment",
            target_type="payable",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={"amount": "1.00", "payee_ref": ref("supplier_account", "account")},
        ),
        "formal",
    )
    assert not plan["executable"] and any(
        "VALID_VERIFICATION_REQUIRED" in b for b in plan["blockers"]
    )
    assert await store.pool.fetchval("SELECT count(*) FROM finance_settlement_operation") == 0


async def test_internal_record_is_never_executable_even_in_simulation(store: Any) -> None:
    """内部记录业务批准不提升为资金/凭证资格，即使模拟环境也一样。"""
    from xuanpu_finance.expenses.models import ActionPreview, ExpensePolicy

    from .test_expense_domain import claim, policy, preview
    from .test_f2_domain import configured, ref

    owner, payer, _, approver = await configured(store)
    data = policy(version=2).model_dump()
    data["record_mode"] = "internal_record"
    await store.configure_expenses([ExpensePolicy(**data)])
    doc = await claim(store, owner, "100.00")
    submit = await preview(store, owner, doc)
    await execute(store, owner, submit)
    doc = await store.get_expense_claim(owner, doc["id"])
    queue = await store.list_review_queue(approver, "e")
    decision = await preview(
        store, approver, doc, "approve", workflow_step_id=queue["items"][0]["workflow_step_id"]
    )
    await execute(store, approver, decision)
    doc = await store.get_expense_claim(owner, doc["id"])
    plan = await store.preview_finance_action(
        payer,
        ActionPreview(
            action="execute_payment",
            target_type="expense_claim",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={"amount": "1.00", "payee_ref": ref("supplier_account", "account")},
        ),
        "internal-no-pay",
    )
    assert not plan["executable"] and any(
        "INTERNAL_RECORD_NOT_EXECUTABLE" in b for b in plan["blockers"]
    )


async def test_same_external_transaction_cannot_be_counted_twice(store: Any) -> None:
    """渠道重复交易号在不同计划也不能累计为第二笔已付款。"""
    from .test_f2_domain import provider_result

    _, payer, _, approver, doc = await approved_payable(store)
    first = await action_plan(store, payer, approver, doc, "execute_payment", "600.00")
    await execute(store, payer, first)
    job = await store.claim_settlement()
    request = await store.prepare_settlement_dispatch(job)
    result = provider_result(request)
    await store.record_settlement_result(job, result, b"first")
    second = await action_plan(store, payer, approver, doc, "execute_payment", "400.00")
    await execute(store, payer, second)
    job = await store.claim_settlement()
    request = await store.prepare_settlement_dispatch(job)
    repeated = provider_result(request)
    repeated["transaction_id"] = result["transaction_id"]
    await store.record_settlement_result(job, repeated, b"repeated-transaction")
    summary = await store.get_payable(payer, doc["id"])
    assert summary["paid_amount"] == "600.00" and summary["payment_pending_amount"] == "400.00"
    assert summary["payment_status"] == "outcome_unknown"
    assert (await store.list_exceptions(payer, "e"))["total"] == 1


async def test_unsent_directory_outage_keeps_record_pending_and_payable_resource_kind(
    store: Any,
) -> None:
    """未发送的身份平台离线只延期，公开记录保持pending且应付资源不冒称报销。"""
    _, payer, _, approver, doc = await approved_payable(store)
    plan = await action_plan(store, payer, approver, doc, "execute_payment")
    operation = await execute(store, payer, plan)
    assert operation["resources"] == [{"kind": "payable", "id": doc["id"]}]
    job = await store.claim_settlement()
    await store.defer_settlement(job, "IDENTITY_UNAVAILABLE", uncertain=False)
    assert (await store.list_payments(payer, "e"))["items"][0]["status"] == "pending"
    assert (await store.get_operation(payer, operation["id"]))["status"] == "running"


async def test_cancel_after_dispatch_marks_record_unknown_without_releasing_amount(
    store: Any,
) -> None:
    """发送后的取消仅核对原请求，操作与付款记录同时未知且占额不释放。"""
    _, payer, _, approver, doc = await approved_payable(store)
    plan = await action_plan(store, payer, approver, doc, "execute_payment")
    operation = await execute(store, payer, plan)
    job = await store.claim_settlement()
    await store.prepare_settlement_dispatch(job)
    latest = await store.get_operation(payer, operation["id"])
    result = await store.cancel_operation(payer, latest["id"], latest["version"], "cancel-sent")
    assert result["status"] == "outcome_unknown"
    assert (await store.list_payments(payer, "e"))["items"][0]["status"] == "outcome_unknown"
    assert (await store.get_payable(payer, doc["id"]))["payment_pending_amount"] == "600.00"


async def test_stale_job_cannot_attach_evidence_to_another_same_provider_operation(
    store: Any,
) -> None:
    """旧租约不能仅改操作ID，把另一请求的原始回执挂到同渠道别的操作。"""
    from .test_f2_domain import provider_result

    _, payer, _, approver, doc = await approved_payable(store)
    plans = [
        await action_plan(store, payer, approver, doc, "execute_payment", amount)
        for amount in ("600.00", "400.00")
    ]
    for plan in plans:
        await execute(store, payer, plan)
    first = await store.claim_settlement()
    request = await store.prepare_settlement_dispatch(first)
    second = await store.claim_settlement()
    await store.prepare_settlement_dispatch(second)
    forged = {**first, "id": second["id"]}
    with pytest.raises(FinanceError, match="STALE_RECEIPT_SCOPE_INVALID"):
        await store.record_settlement_result(forged, provider_result(request), b"wrong-operation")
    assert await store.pool.fetchval("SELECT count(*) FROM finance_settlement_receipt") == 0
    assert (await store.get_payable(payer, doc["id"]))["payment_pending_amount"] == "1000.00"

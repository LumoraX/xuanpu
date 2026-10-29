"""真实PG下红冲回执与提交、审批及付款发送共用蓝票行锁。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ActionPreview, AllocationRequest

from .test_expense_domain import allocate, claim, execute, preview, setup
from .test_f2_domain import action_plan, payable
from .test_f2_domain import configured as settlement_configured
from .test_f3_domain import configured, fresh_snapshot, invoice
from .test_f3_guards import verified

if TYPE_CHECKING:
    from collections.abc import Coroutine


async def issued_result(store: Any, owner: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """先真实受理并冻结请求，再提供可通过领域校验的部分红冲回执。"""
    await store.verify_invoice(owner, "blue", 2, "issued-race")
    job = await store.claim_compliance()
    assert job is not None
    request = await store.prepare_compliance_dispatch(job)
    now = datetime.now(UTC)
    case = {"external_case_id": "partial-red", "confirmation_id": "confirmation", "authority_revision": 2, "status": "issued", "our_side": True, "initiated_at": now.isoformat(), "authority_deadline": (now + timedelta(hours=72)).isoformat(), "blue_invoice_id": "blue", "buyer_tax_id": "BUYER", "seller_tax_id": "SELLER", "amount": "60.00", "tax": "6.00", "red_invoice_number": "red-60", "line_adjustments": {}}
    result = {**{key: value for key, value in request.items() if key != "payload"}, "status": "succeeded", "evidence": [{"id": "issued-proof", "sha256": "b" * 64, "media_type": "application/json", "object_ref": "fixed-provider-object", "size": 2}], "outcome": fresh_snapshot(2, verification_id="issued-verification", red_letter_cases=[case])}
    return job, result


async def blocked_by(conn: Any, blocker_pid: int) -> Any:
    """直接观察PG阻塞图，避免只凭固定睡眠推断事务正在等待。"""
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        row = await conn.fetchrow("SELECT pid,query,wait_event_type FROM pg_stat_activity WHERE datname=current_database() AND $1::integer=ANY(pg_blocking_pids(pid))", blocker_pid)
        if row is not None:
            assert row["wait_event_type"] == "Lock"
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"没有观察到等待PG事务{blocker_pid}的行锁")


async def race_issued(
    store: Any,
    job: dict[str, Any],
    result: dict[str, Any],
    boundary: Coroutine[Any, Any, Any],
) -> FinanceError:
    """用数据库触发器暂停真实结果事务，证明另一写边界等待蓝票锁。"""
    lock_id = int(uuid4().hex[:15], 16)
    await store.pool.execute(f"CREATE FUNCTION pause_issued_result() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock({lock_id}::bigint); RETURN NEW; END $$")
    await store.pool.execute("CREATE TRIGGER pause_issued_result BEFORE UPDATE ON finance_invoice_compliance FOR EACH ROW EXECUTE FUNCTION pause_issued_result()")
    tax_task: asyncio.Task[None] | None = None
    boundary_task: asyncio.Task[Any] | None = None
    async with store.pool.acquire() as gate:
        await gate.execute("SELECT pg_advisory_lock($1::bigint)", lock_id)
        try:
            tax_task = asyncio.create_task(store.record_compliance_result(job, result, json.dumps(result).encode()))
            tax_waiter = await blocked_by(gate, gate.get_server_pid())
            assert "UPDATE finance_invoice_compliance" in tax_waiter["query"]
            boundary_task = asyncio.create_task(boundary)
            boundary_waiter = await blocked_by(gate, tax_waiter["pid"])
            assert "finance_invoice" in boundary_waiter["query"]
            assert "FOR UPDATE" in boundary_waiter["query"]
            assert not tax_task.done() and not boundary_task.done()
        finally:
            await gate.execute("SELECT pg_advisory_unlock($1::bigint)", lock_id)
            if tax_task is not None:
                await asyncio.wait_for(tax_task, 5)
            await store.pool.execute("DROP TRIGGER pause_issued_result ON finance_invoice_compliance")
            await store.pool.execute("DROP FUNCTION pause_issued_result()")
            await store.release_compliance(job)
    assert boundary_task is not None
    with pytest.raises(FinanceError) as rejected:
        await asyncio.wait_for(boundary_task, 5)
    assert await store.pool.fetchval("SELECT status FROM finance_compliance_operation WHERE id=$1", job["id"]) == "succeeded"
    return rejected.value


async def claim_plan(store: Any, owner: Any, approver: Any, *, approve: bool) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    """为提交或最终业务批准构造在红冲前实际可执行的原计划。"""
    doc = await claim(store, owner, amount="80.00")
    await allocate(store, owner, doc, id="blue", amount="80.00")
    doc = await store.get_expense_claim(owner, doc["id"])
    plan = await preview(store, owner, doc)
    assert plan["executable"], plan["blockers"]
    if not approve:
        return doc, owner, plan
    await execute(store, owner, plan)
    doc = await store.get_expense_claim(owner, doc["id"])
    queue = await store.list_review_queue(approver, "e")
    step = next(item for item in queue["items"] if item["target_id"] == doc["id"])
    plan = await preview(store, approver, doc, "approve", workflow_step_id=step["workflow_step_id"])
    assert plan["executable"], plan["blockers"]
    return doc, approver, plan


@pytest.mark.parametrize("approve", [False, True], ids=["submit", "approve"])
async def test_issued_result_serializes_with_f1_submit_and_approve(store: Any, approve: bool) -> None:
    """已发红冲事务持锁时提交或批准等待，提交后严格按新有效余额阻断。"""
    owner, _, _ = await verified(store)
    owner, people = await setup(store)
    owner = replace(owner, origin="human")
    doc, actor, plan = await claim_plan(store, owner, people[0], approve=approve)
    job, result = await issued_result(store, owner)
    error = await race_issued(store, job, result, execute(store, actor, plan))
    assert error.code == ("INVOICE_ALLOCATION_SHORTFALL" if approve else "INVOICE_BALANCE_EXCEEDED")
    current = await store.get_expense_claim(owner, doc["id"])
    assert current["status"] == ("under_review" if approve else "draft")
    assert current["version"] == doc["version"]
    assert await store.pool.fetchval("SELECT state FROM finance_allocation WHERE document_id=$1", doc["id"]) == ("reserved" if approve else "proposed")
    invoice = await store.get_invoice(owner, "blue")
    assert invoice["version"] == 1 and invoice["effective_gross"] == "40.00"
    assert invoice["allocation_shortfall"] is approve


async def approved_blue_payable(store: Any, owner: Any, approver: Any) -> dict[str, Any]:
    """原生应付用同一蓝票完成真实提交和批准，付款账户沿既有供应商配置。"""
    doc = await payable(store, owner, amount="80.00")
    await store.create_invoice_allocation(owner, AllocationRequest(invoice_id="blue", invoice_version=1, document_id=doc["id"], document_kind="payable", expected_document_version=doc["version"], amount="80.00", tax="0.00"), "payable-blue-allocation")
    doc = await store.get_payable(owner, doc["id"])
    submit = await store.preview_finance_action(owner, ActionPreview(action="submit_payable", target_type="payable", target_id=doc["id"], expected_version=doc["version"], parameters={}), "submit-blue-payable")
    assert submit["executable"], submit["blockers"]
    await execute(store, owner, submit)
    doc = await store.get_payable(owner, doc["id"])
    queue = await store.list_review_queue(approver, "e")
    step = next(item for item in queue["items"] if item["target_id"] == doc["id"])
    approval = await store.preview_finance_action(approver, ActionPreview(action="approve", target_type="payable", target_id=doc["id"], expected_version=doc["version"], parameters={"workflow_step_id": step["workflow_step_id"]}), "approve-blue-payable")
    assert approval["executable"], approval["blockers"]
    await execute(store, approver, approval)
    return await store.get_payable(owner, doc["id"])


async def test_issued_result_serializes_with_f2_prepare_and_preserves_unknown_payment(store: Any) -> None:
    """新发送等待红冲写锁后阻断，既有未知付款与原预留历史不得释放或重写。"""
    await verified(store)
    owner, payer, _, approver = await settlement_configured(store)
    doc = await approved_blue_payable(store, owner, approver)
    first_plan = await action_plan(store, payer, approver, doc, "execute_payment", amount="20.00")
    first = await execute(store, payer, first_plan)
    unknown_job = await store.claim_settlement()
    assert unknown_job is not None
    original_request = await store.prepare_settlement_dispatch(unknown_job)
    await store.defer_settlement(unknown_job, "TIMEOUT", uncertain=True)
    await store.release_settlement(unknown_job)
    second_plan = await action_plan(store, payer, approver, doc, "execute_payment", amount="60.00")
    second = await execute(store, payer, second_plan)
    pending_job = await store.claim_settlement()
    assert pending_job is not None and pending_job["id"] == second["id"]
    tax_job, result = await issued_result(store, owner)
    error = await race_issued(store, tax_job, result, store.prepare_settlement_dispatch(pending_job))
    assert error.code == "INVOICE_ALLOCATION_SHORTFALL"
    original = await store.pool.fetchrow("SELECT status,request FROM finance_settlement_operation WHERE id=$1", first["id"])
    assert original["status"] == "outcome_unknown"
    assert json.loads(original["request"]) == original_request
    rejected = await store.pool.fetchrow("SELECT status,request FROM finance_settlement_operation WHERE id=$1", second["id"])
    assert rejected["status"] == "running" and rejected["request"] is None
    assert await store.pool.fetchval("SELECT amount FROM finance_allocation WHERE document_id=$1 AND state='reserved'", doc["id"]) == 80
    current = await store.get_payable(payer, doc["id"])
    assert current["payment_pending_amount"] == "80.00" and current["paid_amount"] == "0.00"
    invoice = await store.get_invoice(owner, "blue")
    assert invoice["allocation_shortfall"] is True and invoice["available_amount"] == "0.00"
    await store.release_settlement(pending_job)


async def test_concurrent_red_case_identifier_cannot_move_between_blue_invoices(store: Any) -> None:
    """两张蓝票同时首次收到同确认号时，只能一笔成功且原件与另一笔未知均保全。"""
    owner,_,_=await configured(store)
    for id in ("blue-one","blue-two"):
        await invoice(store,id)
        await store.verify_invoice(owner,id,1,"verify-"+id)
    jobs=[]
    results=[]
    target_ids=[]
    for _ in range(2):
        job=await store.claim_compliance()
        assert job is not None
        request=await store.prepare_compliance_dispatch(job)
        target_id=request["payload"]["target_snapshot"]["id"]
        now=datetime.now(UTC)
        case={"external_case_id":"same-external-case","confirmation_id":"same-confirmation","authority_revision":1,"status":"awaiting_confirmation","our_side":True,"initiated_at":now.isoformat(),"authority_deadline":(now+timedelta(hours=72)).isoformat(),"blue_invoice_id":target_id,"buyer_tax_id":"BUYER","seller_tax_id":"SELLER","amount":"10.00","tax":"1.00","line_adjustments":{}}
        jobs.append(job)
        target_ids.append(target_id)
        results.append({**{k:v for k,v in request.items() if k != "payload"},"status":"succeeded","evidence":[{"id":"case-proof","sha256":"b"*64,"media_type":"application/json","object_ref":"fixed","size":2}],"outcome":fresh_snapshot(verification_id="v-"+target_id,red_letter_cases=[case])})
    lock_id=int(uuid4().hex[:15],16)
    await store.pool.execute(f"CREATE FUNCTION pause_red_case_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock({lock_id}::bigint); RETURN NEW; END $$")
    await store.pool.execute("CREATE TRIGGER pause_red_case_insert BEFORE INSERT ON finance_red_letter_case FOR EACH ROW EXECUTE FUNCTION pause_red_case_insert()")
    tasks=[]
    async with store.pool.acquire() as gate:
        await gate.execute("SELECT pg_advisory_lock($1::bigint)",lock_id)
        try:
            tasks=[asyncio.create_task(store.record_compliance_result(job,result,json.dumps(result).encode())) for job,result in zip(jobs,results,strict=True)]
            deadline=asyncio.get_running_loop().time()+5
            while asyncio.get_running_loop().time()<deadline:
                waiting=await gate.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND $1::integer=ANY(pg_blocking_pids(pid))",gate.get_server_pid())
                if waiting == 2:
                    break
                await asyncio.sleep(0.01)
            assert waiting == 2,"两个事务必须在各自查询外部确认号为空后同时抵达INSERT"
            assert all(not task.done() for task in tasks)
        finally:
            await gate.execute("SELECT pg_advisory_unlock($1::bigint)",lock_id)
            await asyncio.wait_for(asyncio.gather(*tasks),5)
            await store.pool.execute("DROP TRIGGER pause_red_case_insert ON finance_red_letter_case")
            await store.pool.execute("DROP FUNCTION pause_red_case_insert()")
    operations=[await store.get_operation(owner,job["id"]) for job in jobs]
    assert sorted(op["status"] for op in operations) == ["outcome_unknown","succeeded"]
    rejected=next(op for op in operations if op["status"] == "outcome_unknown")
    assert rejected["last_error"] == "RED_CASE_SCOPE_MISMATCH"
    red_case=await store.pool.fetchrow("SELECT invoice_id,data FROM finance_red_letter_case WHERE external_case_id='same-external-case'")
    assert red_case["invoice_id"] == json.loads(red_case["data"])["blue_invoice_id"]
    for job,target_id in zip(jobs,target_ids,strict=True):
        assert await store.pool.fetchval("SELECT count(*) FROM finance_compliance_receipt WHERE operation_id=$1",job["id"]) == 1
        assert (await store.get_invoice_compliance(owner,target_id))["version"] == (2 if target_id == red_case["invoice_id"] else 1)
        await store.release_compliance(job)

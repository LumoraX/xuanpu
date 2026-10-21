"""F3实际人工、来源隔离、权威事实与红冲并发守卫。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from xuanpu_finance.compliance.models import ComplianceActionPreview
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ActionExecution
from xuanpu_finance.models import FinancePrincipal

from .test_f3_domain import configured, fresh_snapshot, invoice, result_for


async def verified(store: Any) -> tuple[FinancePrincipal, FinancePrincipal, FinancePrincipal]:
    """创建真实队列接受过的有效隔离查验。"""
    people = await configured(store)
    await invoice(store)
    await store.verify_invoice(people[0], "blue", 1, "initial")
    job = await store.claim_compliance()
    assert job is not None
    await result_for(store, job, fresh_snapshot(verification_id="verification"))
    return people


async def usage_plan(store: Any, actor: FinancePrincipal) -> dict[str, Any]:
    """经办人准备不可变用途确认计划。"""
    return await store.preview_finance_action(actor, ComplianceActionPreview(action="confirm_usage", target_type="invoice", target_id="blue", expected_version=(await store.get_invoice_compliance(actor, "blue"))["version"], parameters={"accounting_period": "2026-10"}), "usage")


async def approve(store: Any, actor: FinancePrincipal, plan: dict[str, Any]) -> dict[str, Any]:
    """实际审批人通过原行动计划的明确人工决定批准。"""
    review = await store.preview_finance_action(actor, ComplianceActionPreview(action="approve", target_type="action_plan", target_id=plan["id"], expected_version=1, parameters={"workflow_step_id": plan["workflow"]["steps"][0]["id"]}), "approve-" + plan["id"])
    return await store.execute_finance_action(actor, ActionExecution(plan_id=review["id"], plan_digest=review["digest"]), "decide-" + plan["id"])


async def test_actual_approver_can_read_only_assigned_invoice_and_execute_requires_human(store: Any) -> None:
    """F3指派具有必要证据读取权，模型无法使用已有批准执行。"""
    owner, tax, auditor = await verified(store)
    plan = await usage_plan(store, tax)
    plan = await store.get_action_plan(tax, plan["id"])
    assert plan["executable"] is False
    assert plan["workflow"]["status"] == "under_review"
    assert (await store.get_invoice_compliance(auditor, "blue"))["verification_status"] == "passed"
    await approve(store, auditor, plan)
    approved = await store.get_action_plan(tax, plan["id"])
    assert approved["executable"] is True
    with pytest.raises(FinanceError, match="HUMAN_REQUIRED"):
        await store.execute_finance_action(replace(tax, origin="agent"), ActionExecution(plan_id=plan["id"], plan_digest=plan["digest"]), "agent")
    result = await store.execute_finance_action(tax, ActionExecution(plan_id=plan["id"], plan_digest=plan["digest"]), "human")
    assert result["status"] == "accepted"
    assert (await store.list_review_queue(auditor, "e"))["total"] == 0
    assert owner.user_id != tax.user_id


async def test_missing_verification_identifier_and_future_fact_do_not_advance_state(store: Any) -> None:
    """通过必须有实际查验号，未来事实也不能洗成有效查验。"""
    owner, _, _ = await configured(store)
    await invoice(store)
    for key, snapshot in [("missing", fresh_snapshot(verification_id=None)), ("future", fresh_snapshot(2, verification_id="v", observed_at=(datetime.now(UTC)+timedelta(days=1)).isoformat(), valid_until=(datetime.now(UTC)+timedelta(days=2)).isoformat()))]:
        await store.verify_invoice(owner, "blue", 1, key)
        job = await store.claim_compliance()
        assert job is not None
        await result_for(store, job, snapshot)
    state = await store.get_invoice_compliance(owner, "blue")
    assert state["version"] == 1
    assert state["verification_status"] == "not_checked"
    assert await store.pool.fetchval("SELECT count(*) FROM finance_compliance_receipt") == 2


async def test_invalid_red_case_rolls_back_all_materialized_fact_but_keeps_raw(store: Any) -> None:
    """严格快照中的非法主体不能留下部分合规推进。"""
    owner, _, _ = await configured(store)
    await invoice(store)
    await store.verify_invoice(owner, "blue", 1, "v")
    job = await store.claim_compliance()
    assert job is not None
    now = datetime.now(UTC)
    case = {"external_case_id": "bad", "confirmation_id": "confirm", "authority_revision": 1, "status": "awaiting_confirmation", "our_side": True, "initiated_at": now.isoformat(), "authority_deadline": (now+timedelta(hours=72)).isoformat(), "blue_invoice_id": "other-blue", "buyer_tax_id": "BUYER", "seller_tax_id": "SELLER", "amount": "10.00", "tax": "1.00", "line_adjustments": {}}
    await result_for(store, job, fresh_snapshot(verification_id="v", red_letter_cases=[case]))
    assert (await store.get_invoice_compliance(owner, "blue"))["version"] == 1
    assert await store.pool.fetchval("SELECT count(*) FROM finance_red_letter_case") == 0
    assert await store.pool.fetchval("SELECT count(*) FROM finance_compliance_receipt") == 1


async def test_revoked_then_restored_approver_cannot_resurrect_original_plan(store: Any) -> None:
    """当前角色相同仍需匹配原岗位代数。"""
    _, tax, auditor = await verified(store)
    plan = await usage_plan(store, tax)
    await store.grant("t", auditor.user_id, "e", "collector")
    await store.grant("t", auditor.user_id, "e", "approver")
    current = await store.get_action_plan(tax, plan["id"])
    assert any("PLAN_BINDINGS_CHANGED" in blocker for blocker in current["blockers"])
    assert current["executable"] is False


async def test_pending_snapshot_preserves_unexpired_passed_and_original_ttl(store: Any) -> None:
    """查询pending可以推进权威版本但不覆盖尚未过期的查验事实。"""
    owner, _, _ = await verified(store)
    before = await store.get_invoice_compliance(owner, "blue")
    await store.verify_invoice(owner, "blue", 2, "pending")
    job = await store.claim_compliance()
    assert job is not None
    await result_for(store, job, fresh_snapshot(2, verification_status="pending"))
    after = await store.get_invoice_compliance(owner, "blue")
    assert after["authority_revision"] == 2
    assert after["verification_status"] == "passed"
    assert after["valid_until"] == before["valid_until"]


async def test_foreign_tenant_and_company_deny_invoice_and_cursor_reuse(store: Any) -> None:
    """公司的实际授权不能变成跨租户或跨公司读取权。"""
    owner, _, _ = await verified(store)
    with pytest.raises(FinanceError, match="RESOURCE_NOT_FOUND"):
        await store.get_invoice_compliance(FinancePrincipal("other", "owner"), "blue")
    await store.seed_company("t", "other-company", "其他", "OTHER")
    await store.grant("t", "other-user", "other-company", "reviewer")
    with pytest.raises(FinanceError, match="COMPANY_FORBIDDEN"):
        await store.get_invoice_compliance(FinancePrincipal("t", "other-user"), "blue")
    one = await store.list_compliance_plans(owner, "e", limit=1)
    assert one["total"] == 1


async def test_stale_worker_cannot_advance_authority_or_steal_another_operation(store: Any) -> None:
    """过期租约只保全曾属于自身原请求的stale原件。"""
    owner, _, _ = await configured(store)
    await invoice(store)
    await store.verify_invoice(owner, "blue", 1, "v")
    first = await store.claim_compliance()
    assert first is not None
    request = await store.prepare_compliance_dispatch(first)
    await store.pool.execute("UPDATE finance_compliance_operation SET lease_until=now()-interval '1 second'")
    second = await store.claim_compliance()
    assert second is not None and second["mode"] == "query"
    result = {**{k:v for k,v in request.items() if k != "payload"}, "status": "succeeded", "evidence": [{"id":"proof","sha256":"b"*64,"media_type":"application/json","object_ref":"object"}], "outcome": fresh_snapshot(verification_id="v")}
    await store.record_compliance_result(first, result, json.dumps(result).encode())
    assert (await store.get_invoice_compliance(owner, "blue"))["version"] == 1
    assert await store.pool.fetchval("SELECT count(*) FROM finance_compliance_receipt WHERE stale") == 1


async def test_tax_snapshot_and_new_allocation_serialize_on_the_same_invoice_lock(store: Any) -> None:
    """真实PG并发确认红冲和分票时，新提议看到红冲后的有效额度。"""
    from .test_expense_domain import allocate, claim, setup

    owner, _, _ = await verified(store)
    await setup(store)
    document = await claim(store, replace(owner, origin="human"), amount="80.00")
    now = datetime.now(UTC)
    case = {"external_case_id":"red", "confirmation_id":"c", "authority_revision":1,"status":"issued","our_side":True,"initiated_at":now.isoformat(),"authority_deadline":(now+timedelta(hours=72)).isoformat(),"blue_invoice_id":"blue","buyer_tax_id":"BUYER","seller_tax_id":"SELLER","amount":"60.00","tax":"6.00","red_invoice_number":"red-1","line_adjustments":{}}
    await store.verify_invoice(owner, "blue", 2, "red")
    job = await store.claim_compliance()
    assert job is not None
    await store.prepare_compliance_dispatch(job)
    async with store.pool.acquire() as conn, conn.transaction():
        await conn.fetchrow("SELECT * FROM finance_invoice WHERE id='blue' FOR UPDATE")
        task = asyncio.create_task(allocate(store, owner, document, id="blue", amount="80.00"))
        await asyncio.sleep(0.03)
        assert not task.done()
        await conn.execute("INSERT INTO finance_red_letter_case(tenant_id,legal_entity_id,id,owner_id,invoice_id,external_case_id,authority_revision,status,data) VALUES('t','e','red','owner','blue','red',1,'issued',$1::jsonb)", json.dumps(case))
    with pytest.raises(FinanceError, match="ALLOCATION_LIMIT"):
        await task


async def test_nonterminal_query_results_reach_manual_limit_without_resubmitting(store: Any) -> None:
    """渠道持续pending也必须遵守四次定时GET上限。"""
    owner, _, _ = await configured(store)
    await invoice(store)
    await store.verify_invoice(owner,"blue",1,"v")
    job=await store.claim_compliance()
    assert job is not None
    request=await store.prepare_compliance_dispatch(job)
    response={**{k:v for k,v in request.items() if k != "payload"},"status":"pending","evidence":[]}
    await store.record_compliance_result(job,response,json.dumps(response).encode())
    await store.release_compliance(job)
    for _ in range(4):
        await store.pool.execute("UPDATE finance_compliance_operation SET next_query_at=now()")
        current=await store.claim_compliance(allow_dispatch=False)
        assert current is not None and current["mode"] == "query"
        await store.record_compliance_result(current,response,json.dumps(response).encode())
        await store.release_compliance(current)
    row=await store.pool.fetchrow("SELECT query_count,next_query_at FROM finance_compliance_operation")
    assert row["query_count"] == 4
    assert row["next_query_at"] is None
    assert await store.claim_compliance(allow_dispatch=False) is None


async def test_original_red_confirmation_deadline_and_identity_are_not_refreshable(store: Any) -> None:
    """已有确认单超原72小时后不可因重新查询而再获确认资格。"""
    owner,tax,_=await configured(store)
    await invoice(store)
    now=datetime.now(UTC)
    case={"external_case_id":"red","confirmation_id":"authority-id","authority_revision":1,"status":"awaiting_confirmation","our_side":True,"initiated_at":(now-timedelta(hours=73)).isoformat(),"authority_deadline":(now+timedelta(hours=12)).isoformat(),"blue_invoice_id":"blue","buyer_tax_id":"BUYER","seller_tax_id":"SELLER","amount":"40.00","tax":"4.00","line_adjustments":{}}
    await store.verify_invoice(owner,"blue",1,"v")
    job=await store.claim_compliance()
    assert job is not None
    await result_for(store,job,fresh_snapshot(verification_id="v",red_letter_cases=[case]))
    red=(await store.list_red_letter_cases(tax,"e"))["items"][0]
    plan=await store.preview_finance_action(tax,ComplianceActionPreview(action="confirm_red_letter",target_type="red_letter_case",target_id=red["id"],expected_version=red["version"],parameters={"confirmation_id":"authority-id"}),"red-confirm")
    assert any("RED_CONFIRMATION_EXPIRED" in blocker for blocker in plan["blockers"])
    mismatch=await store.preview_finance_action(tax,ComplianceActionPreview(action="confirm_red_letter",target_type="red_letter_case",target_id=red["id"],expected_version=red["version"],parameters={"confirmation_id":"forged"}),"red-forged")
    assert any("RED_CONFIRMATION_MISMATCH" in blocker for blocker in mismatch["blockers"])


async def test_confirmed_red_case_does_not_reduce_amount_until_issued_and_preserves_shortfall(store: Any) -> None:
    """确认不等开票，多次部分红冲扣额而不释放历史预留。"""
    from .test_expense_domain import allocate, claim, setup

    owner,_,_=await verified(store)
    await setup(store)
    document=await claim(store,replace(owner,origin="human"),amount="80.00")
    await allocate(store,owner,document,id="blue",amount="80.00",tax="8.00")
    await store.pool.execute("UPDATE finance_allocation SET state='reserved'")
    now=datetime.now(UTC)
    base={"external_case_id":"red","confirmation_id":"c","authority_revision":1,"status":"confirmed","our_side":True,"initiated_at":now.isoformat(),"authority_deadline":(now+timedelta(hours=72)).isoformat(),"blue_invoice_id":"blue","buyer_tax_id":"BUYER","seller_tax_id":"SELLER","amount":"30.00","tax":"3.00","line_adjustments":{}}
    for revision,status in [(2,"confirmed"),(3,"issued")]:
        version=(await store.get_invoice_compliance(owner,"blue"))["version"]
        await store.verify_invoice(owner,"blue",version,"red-"+status)
        job=await store.claim_compliance()
        assert job is not None
        await result_for(store,job,fresh_snapshot(revision,verification_id="v",red_letter_cases=[{**base,"authority_revision":revision,"status":status,"red_invoice_number":"red-invoice" if status == "issued" else None}]))
        current=await store.get_invoice_compliance(owner,"blue")
        if status == "confirmed":
            assert current["effective_gross"] == "100.00"
            assert current["available_amount"] == "20.00"
    assert current["effective_gross"] == "70.00"
    assert current["effective_tax"] == "7.00"
    assert current["available_amount"] == "0.00"
    assert current["allocation_shortfall"] is True
    assert await store.pool.fetchval("SELECT state FROM finance_allocation") == "reserved"


async def test_review_cannot_attach_arbitrary_company_evidence(store: Any) -> None:
    """人工批准的材料引用也必须属于固定目标的实际原件。"""
    _,tax,auditor=await verified(store)
    plan=await usage_plan(store,tax)
    plan=await store.get_action_plan(tax,plan["id"])
    review=await store.preview_finance_action(auditor,ComplianceActionPreview(action="approve",target_type="action_plan",target_id=plan["id"],expected_version=1,parameters={"workflow_step_id":plan["workflow"]["steps"][0]["id"],"evidence_ids":["forged"]}),"forged-evidence")
    assert any("EVIDENCE_FORBIDDEN" in blocker for blocker in review["blockers"])


async def test_inflight_tax_dimension_blocks_a_second_distinct_approved_plan(store: Any) -> None:
    """原确认未知结果仍占用同票同维度，换计划和幂等键不得再次发出。"""
    _,tax,auditor=await verified(store)
    plan=await usage_plan(store,tax)
    plan=await store.get_action_plan(tax,plan["id"])
    other=await store.preview_finance_action(tax,ComplianceActionPreview(action="confirm_usage",target_type="invoice",target_id="blue",expected_version=2,parameters={"accounting_period":"2026-10"}),"other-plan")
    other=await store.get_action_plan(tax,other["id"])
    await approve(store,auditor,plan)
    await approve(store,auditor,other)
    await store.execute_finance_action(tax,ActionExecution(plan_id=plan["id"],plan_digest=plan["digest"]),"first")
    job=await store.claim_compliance()
    assert job is not None
    await store.prepare_compliance_dispatch(job)
    await store.defer_compliance(job,"TIMEOUT",uncertain=True)
    await store.release_compliance(job)
    with pytest.raises(FinanceError,match="COMPLIANCE_DIMENSION_INFLIGHT"):
        await store.execute_finance_action(tax,ActionExecution(plan_id=other["id"],plan_digest=other["digest"]),"second")


async def test_cumulative_issued_red_amount_and_tax_cannot_exceed_blue_invoice(store: Any) -> None:
    """权威快照内部累计红冲超额不可被max0悄然吞掉。"""
    owner,_,_=await verified(store)
    now=datetime.now(UTC)
    base={"confirmation_id":"c","authority_revision":2,"status":"issued","our_side":True,"initiated_at":now.isoformat(),"authority_deadline":(now+timedelta(hours=72)).isoformat(),"blue_invoice_id":"blue","buyer_tax_id":"BUYER","seller_tax_id":"SELLER","amount":"60.00","tax":"6.00","red_invoice_number":"red","line_adjustments":{}}
    cases=[{**base,"external_case_id":"red-"+str(i),"confirmation_id":"c-"+str(i)} for i in range(2)]
    await store.verify_invoice(owner,"blue",2,"excess")
    job=await store.claim_compliance()
    assert job is not None
    await result_for(store,job,fresh_snapshot(2,verification_id="v",red_letter_cases=cases))
    state=await store.get_invoice_compliance(owner,"blue")
    assert state["version"] == 2
    assert state["effective_gross"] == "100.00"
    assert await store.pool.fetchval("SELECT count(*) FROM finance_red_letter_case") == 0
    assert (await store.get_operation(owner,job["id"]))["last_error"] == "RED_ISSUED_BALANCE_EXCEEDED"


async def test_archive_document_cannot_upgrade_historical_formal_invoice_to_simulation(store: Any) -> None:
    """用新模拟单据包装旧正式原件，也不能越过原件来源隔离。"""
    from .test_expense_domain import allocate, claim, setup

    owner,tax,_=await configured(store)
    await setup(store)
    await invoice(store,simulation=False)
    document=await claim(store,owner,"20.00")
    await allocate(store,owner,document,id="blue",amount="20.00")
    await store.pool.execute("UPDATE finance_allocation SET state='reserved'")
    current=await store.get_expense_claim(owner,document["id"])
    plan=await store.preview_finance_action(tax,ComplianceActionPreview(action="archive",target_type="expense_claim",target_id=document["id"],expected_version=current["version"],parameters={}),"legacy-envelope")
    assert any("SIMULATION_MODE_CONFLICT" in value for value in plan["blockers"])


async def adjustment_with_formal_material(store: Any) -> tuple[FinancePrincipal, dict[str, Any]]:
    """同公司模拟蓝票有真实调整义务，但候选补件保留历史正式来源。"""
    from .test_f3_archive_domain import originals

    owner,tax,_=await verified(store)
    await originals(store)
    await store.verify_invoice(owner,"blue",2,"adjustment-isolation")
    job=await store.claim_compliance()
    assert job is not None
    await result_for(store,job,fresh_snapshot(2,verification_id="v",adjustments=[{"external_adjustment_id":"adjust","status":"required","reason":"需要补件","amount":"10.00","tax":"1.00"}]))
    await store.pool.execute("INSERT INTO finance_operation(tenant_id,legal_entity_id,id,owner_id,idempotency_key,request_hash,trace_id,source_simulation) VALUES('t','e','formal-import','owner','formal-import','hash','trace',false)")
    await store.pool.execute("INSERT INTO finance_item(tenant_id,legal_entity_id,operation_id,position,file_ref,state,object_key,actual_sha256,content_type,size_bytes) VALUES('t','e','formal-import',0,'{}','parsed','encrypted/formal',$1,'application/pdf',12)","f"*64)
    await store.pool.execute("INSERT INTO finance_receipt VALUES('t','e','formal-material','owner','formal-import',0,'encrypted/formal',$1,'{}')","f"*64)
    return tax,(await store.list_tax_adjustments(tax,"e"))["items"][0]


async def test_simulation_adjustment_rejects_historical_formal_material_at_entry(store: Any) -> None:
    """人工补件不能以同公司和非空摘要将历史正式原件升级成模拟来源。"""
    from xuanpu_finance.compliance.models import TaxAdjustmentEvidence

    tax,adjustment=await adjustment_with_formal_material(store)
    with pytest.raises(FinanceError,match="EVIDENCE_SIMULATION_CONFLICT"):
        await store.record_tax_adjustment_evidence(tax,adjustment["id"],TaxAdjustmentEvidence(expected_version=1,reason="历史正式材料",evidence_ids=["formal-material"]),"reject-formal")
    assert await store.pool.fetchval("SELECT count(*) FROM finance_tax_adjustment_evidence") == 0
    assert await store.pool.fetchval("SELECT version FROM finance_tax_adjustment") == 1
    assert await store.pool.fetchval("SELECT source_simulation FROM finance_operation WHERE id='formal-import'") is False


async def test_archive_closure_rechecks_legacy_adjustment_material_environment(store: Any) -> None:
    """即使旧记录已经引用正式补件，闭包加载也必须阻断跨环境材料。"""
    from .test_f3_archive_domain import archive_plan

    tax,adjustment=await adjustment_with_formal_material(store)
    await store.pool.execute("INSERT INTO finance_tax_adjustment_evidence(tenant_id,legal_entity_id,id,adjustment_id,actor_id,data) VALUES('t','e','legacy-material',$1,'tax',$2::jsonb)",adjustment["id"],json.dumps({"evidence_ids":["formal-material"],"reason":"旧版已记录材料"}))
    plan=await archive_plan(store,tax)
    assert any("ARCHIVE_ADJUSTMENT_ENVIRONMENT_CONFLICT" in value for value in plan["blockers"])
    assert plan["executable"] is False
    assert await store.pool.fetchval("SELECT count(*) FROM finance_archive") == 0
    assert await store.pool.fetchval("SELECT source_simulation FROM finance_operation WHERE id='formal-import'") is False


async def test_external_tax_adjustment_identifier_cannot_change_its_original_invoice(store: Any) -> None:
    """相同外部调整号不能跨蓝票覆盖义务，冲突只保全回执并进入未知。"""
    owner,_,_=await verified(store)
    adjustment={"external_adjustment_id":"same-adjustment","status":"required","reason":"原蓝票义务","amount":"10.00","tax":"1.00"}
    await store.verify_invoice(owner,"blue",2,"first-adjustment")
    first=await store.claim_compliance()
    assert first is not None
    await result_for(store,first,fresh_snapshot(2,verification_id="first-v",adjustments=[adjustment]))
    await invoice(store,"blue-two")
    await store.verify_invoice(owner,"blue-two",1,"conflicting-adjustment")
    conflicting=await store.claim_compliance()
    assert conflicting is not None
    await result_for(store,conflicting,fresh_snapshot(verification_id="second-v",adjustments=[{**adjustment,"amount":"20.00","tax":"2.00","reason":"另一蓝票覆盖"}]))
    operation=await store.get_operation(owner,conflicting["id"])
    assert operation["status"] == "outcome_unknown"
    assert operation["last_error"] == "TAX_ADJUSTMENT_SCOPE_MISMATCH"
    original=await store.pool.fetchrow("SELECT invoice_id,version,data FROM finance_tax_adjustment WHERE external_adjustment_id='same-adjustment'")
    assert original["invoice_id"] == "blue" and original["version"] == 1
    assert json.loads(original["data"]) == {**adjustment,"accounting_period":None}
    assert (await store.get_invoice_compliance(owner,"blue-two"))["version"] == 1
    assert await store.pool.fetchval("SELECT count(*) FROM finance_compliance_receipt WHERE operation_id=$1",conflicting["id"]) == 1


async def formal_original_exception(store: Any) -> tuple[FinancePrincipal, str]:
    """保留历史正式材料缺失异常及其实际正式原件操作。"""
    tax,_=await adjustment_with_formal_material(store)
    await store.pool.execute("INSERT INTO finance_exception(tenant_id,legal_entity_id,id,owner_id,operation_id,item_position,rule_id,receipt_id) VALUES('t','e','formal-exception','owner','formal-import',0,'ORIGINAL_REQUIRED','formal-material')")
    return FinancePrincipal("t","owner"),tax.user_id


async def legacy_received_import(store: Any, *, source: str | None) -> dict[str, Any]:
    """构造旧版已经受理并收到新模拟原件的持久检查点以验证worker兜底。"""
    await store.pool.execute("UPDATE finance_operation SET status='succeeded'")
    await store.pool.execute("INSERT INTO finance_operation(tenant_id,legal_entity_id,id,owner_id,idempotency_key,request_hash,trace_id,source_exception_id,expected_exception_version,source_simulation) VALUES('t','e','new-import','owner','new-import','hash','trace',$1,1,true)",source)
    await store.pool.execute("INSERT INTO finance_item(tenant_id,legal_entity_id,operation_id,position,file_ref,state,object_key,actual_sha256,content_type,size_bytes) VALUES('t','e','new-import',0,'{}','received','encrypted/new',$1,'application/xml',12)","c"*64)
    await store.pool.execute("INSERT INTO finance_receipt VALUES('t','e','new-receipt','owner','new-import',0,'encrypted/new',$1,'{}')","c"*64)
    await store.pool.execute("UPDATE finance_item SET receipt_id='new-receipt' WHERE operation_id='new-import'")
    claim=await store.claim_import()
    assert claim is not None and claim["id"] == "new-import"
    return claim


async def test_simulation_import_cannot_supplement_a_historical_formal_exception(store: Any) -> None:
    """受理补件须核对源异常实际操作来源，不能把正式异常接到模拟原件。"""
    from xuanpu_finance.models import FileRef

    owner,_=await formal_original_exception(store)
    before=await store.pool.fetchval("SELECT count(*) FROM finance_operation")
    with pytest.raises(FinanceError,match="SIMULATION_MODE_CONFLICT"):
        await store.accept_import(owner,"e",[FileRef(file_id="new",revision=1,sha256="c"*64)],"mixed-supplement",source_exception_id="formal-exception",expected_exception_version=1)
    assert await store.pool.fetchval("SELECT count(*) FROM finance_operation") == before
    assert await store.pool.fetchval("SELECT status FROM finance_exception WHERE id='formal-exception'") == "open"


async def test_worker_rechecks_source_environment_for_legacy_accepted_supplement(store: Any) -> None:
    """旧受理记录不能跳过实际提交前来源核对，也不能关联旧正式材料。"""
    from xuanpu_finance.invoices.parser import ParsedInvoice

    await formal_original_exception(store)
    claim=await legacy_received_import(store,source="formal-exception")
    data=json.loads(await store.pool.fetchval("SELECT data FROM finance_invoice WHERE id='blue'"))
    parsed=ParsedInvoice({**data,"number":"new-supplement-invoice"},[],[])
    with pytest.raises(FinanceError,match="SIMULATION_MODE_CONFLICT"):
        await store.finish_item_parsed(claim,0,parsed)
    assert await store.pool.fetchval("SELECT count(*) FROM finance_invoice") == 1
    assert await store.pool.fetchval("SELECT status FROM finance_exception WHERE id='formal-exception'") == "open"
    assert await store.pool.fetchval("SELECT count(*) FROM finance_invoice_receipt WHERE receipt_id='formal-material'") == 0
    await store.release_import(claim)


async def test_archive_original_closure_rejects_historical_cross_environment_receipt_link(store: Any) -> None:
    """即使旧版已将正式receipt挂上模拟票，归档原件闭包也拒绝读取。"""
    from .test_f3_archive_domain import archive_plan

    tax,_=await adjustment_with_formal_material(store)
    await store.pool.execute("INSERT INTO finance_invoice_receipt VALUES('t','e','blue','formal-material')")
    plan=await archive_plan(store,tax)
    assert any("ARCHIVE_ORIGINAL_ENVIRONMENT_CONFLICT" in value for value in plan["blockers"])
    assert plan["executable"] is False


async def test_duplicate_import_cannot_link_simulation_original_to_historical_formal_invoice(store: Any) -> None:
    """内容完全相同仍不能将模拟原件链接到历史正式票据。"""
    from xuanpu_finance.invoices.parser import ParsedInvoice
    from xuanpu_finance.operations import request_digest

    await configured(store)
    data=await invoice(store,simulation=False)
    parsed=ParsedInvoice(data,[],[])
    await store.pool.execute("UPDATE finance_invoice SET canonical_digest=$1 WHERE id='blue'",request_digest({"invoice":parsed.invoice,"lines":parsed.lines}))
    claim=await legacy_received_import(store,source=None)
    with pytest.raises(FinanceError,match="SIMULATION_MODE_CONFLICT"):
        await store.finish_item_parsed(claim,0,parsed)
    assert await store.pool.fetchval("SELECT count(*) FROM finance_invoice_receipt") == 0
    assert await store.pool.fetchval("SELECT source_simulation FROM finance_invoice WHERE id='blue'") is False
    await store.release_import(claim)

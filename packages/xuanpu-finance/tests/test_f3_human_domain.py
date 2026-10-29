"""真实人工红票关联、调整补件和补包在途占用的领域验收。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from xuanpu_finance.compliance.models import (
    ComplianceActionPreview,
    RedLetterInvoiceLink,
    TaxAdjustmentEvidence,
)
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ActionExecution

from .test_f3_archive_domain import (
    accept_archive,
    archive_plan,
    originals,
    outcome_for,
    save_archive_result,
)
from .test_f3_domain import fresh_snapshot, invoice, result_for
from .test_f3_guards import approve, verified


async def test_red_invoice_is_independently_verified_before_human_link_and_never_reused(store: Any) -> None:
    """已确认不能当已开，实际红票必须独立原件、有效查验和真人关联。"""
    owner,tax,_=await verified(store)
    await originals(store)
    now=datetime.now(UTC)
    case={"external_case_id":"red","confirmation_id":"c","authority_revision":2,"status":"issued","our_side":True,"initiated_at":now.isoformat(),"authority_deadline":(now+timedelta(hours=72)).isoformat(),"blue_invoice_id":"blue","buyer_tax_id":"BUYER","seller_tax_id":"SELLER","amount":"100.00","tax":"10.00","red_invoice_number":"red","line_adjustments":{},"line_tax_adjustments":{}}
    await store.verify_invoice(owner,"blue",2,"red-issued")
    job=await store.claim_compliance()
    assert job is not None
    await result_for(store,job,fresh_snapshot(2,verification_id="blue-v",red_letter_cases=[case]))
    red_case=(await store.list_red_letter_cases(tax,"e"))["items"][0]
    await invoice(store,"red",is_red=True)
    await originals(store,"red")
    body=RedLetterInvoiceLink(expected_case_version=red_case["version"],blue_invoice_id="blue",expected_blue_invoice_version=1,red_invoice_id="red",expected_red_invoice_version=1)
    with pytest.raises(FinanceError,match="VALID_VERIFICATION_REQUIRED"):
        await store.link_red_letter_invoice(tax,red_case["id"],body,"link-before-verify")
    await store.verify_invoice(owner,"red",1,"verify-red")
    red_job=await store.claim_compliance()
    assert red_job is not None
    await result_for(store,red_job,fresh_snapshot(verification_id="red-v"))
    linked=await store.link_red_letter_invoice(tax,red_case["id"],body,"link")
    assert linked["status"] == "linked"
    assert linked["authority_status"] == "issued"
    assert await store.link_red_letter_invoice(tax,red_case["id"],body,"link") == linked
    assert await store.pool.fetchval("SELECT count(*) FROM finance_red_letter_link") == 1
    with pytest.raises(FinanceError,match="RED_INVOICE_SCOPE_MISMATCH|RED_INVOICE_NOT_LINKABLE"):
        await store.link_red_letter_invoice(tax,red_case["id"],body,"reuse")


async def test_adjustment_material_received_does_not_become_declaration_success(store: Any) -> None:
    """人工补件保留原权威required状态，同时追加材料收到事实。"""
    owner,tax,_=await verified(store)
    await originals(store)
    await store.verify_invoice(owner,"blue",2,"adjustment")
    job=await store.claim_compliance()
    assert job is not None
    await result_for(store,job,fresh_snapshot(2,verification_id="v",adjustments=[{"external_adjustment_id":"adjust","status":"required","reason":"需要转出材料","amount":"10.00","tax":"1.00","accounting_period":"2026-10"}]))
    adjustment=(await store.list_tax_adjustments(tax,"e"))["items"][0]
    result=await store.record_tax_adjustment_evidence(tax,adjustment["id"],TaxAdjustmentEvidence(expected_version=1,reason="实际收到补件",evidence_ids=["original-blue"]),"material")
    assert result["material_status"] == "received"
    assert result["status"] == "required"
    assert result["declaration_status"] == "not_checked"
    current=(await store.list_tax_adjustments(tax,"e"))["items"][0]
    assert current["status"] == "required"
    assert current["version"] == 2


async def test_same_parent_event_cannot_dispatch_two_supplements_while_first_unknown(store: Any) -> None:
    """补包占用父清册与来源事件，unknown不能换计划重封。"""
    owner,tax,auditor=await verified(store)
    await originals(store)
    job,request=await accept_archive(store,tax,auditor)
    await save_archive_result(store,job,request,outcome_for(request))
    archive=(await store.list_archives(tax,"e"))["items"][0]
    await store.verify_invoice(owner,"blue",2,"new-event")
    tax_job=await store.claim_compliance()
    assert tax_job is not None
    await result_for(store,tax_job,fresh_snapshot(2,verification_id="v2"))
    archive=await store.get_archive(tax,archive["id"])
    params={"parent_manifest_hash":archive["manifest_hash"],"source_event_ids":archive["supplement_event_ids"]}
    first=await archive_plan(store,tax,parent=archive["manifest_hash"],events=archive["supplement_event_ids"])
    second=await store.preview_finance_action(tax,ComplianceActionPreview(action="archive",target_type="invoice",target_id="blue",expected_version=3,parameters=params),"second-supplement")
    second=await store.get_action_plan(tax,second["id"])
    for plan in (first,second):
        await approve(store,auditor,plan)
    await store.execute_finance_action(tax,ActionExecution(plan_id=first["id"],plan_digest=first["digest"]),"first-supplement")
    current=await store.claim_compliance()
    assert current is not None
    await store.prepare_compliance_dispatch(current)
    await store.defer_compliance(current,"TIMEOUT",uncertain=True)
    await store.release_compliance(current)
    with pytest.raises(FinanceError,match="COMPLIANCE_DIMENSION_INFLIGHT"):
        await store.execute_finance_action(tax,ActionExecution(plan_id=second["id"],plan_digest=second["digest"]),"second-supplement")


async def test_archive_evidence_closure_contains_linked_red_adjustment_material_and_real_tax_decision(store: Any) -> None:
    """冻结闭包包含真实红票来源、人工补件和此前已执行税务人工链。"""
    import json

    from .test_f3_guards import usage_plan

    owner,tax,auditor=await verified(store)
    await originals(store)
    plan=await usage_plan(store,tax)
    plan=await store.get_action_plan(tax,plan["id"])
    await approve(store,auditor,plan)
    await store.execute_finance_action(tax,ActionExecution(plan_id=plan["id"],plan_digest=plan["digest"]),"usage")
    tax_job=await store.claim_compliance()
    assert tax_job is not None
    await result_for(store,tax_job,fresh_snapshot(2,verification_id="usage-v",usage_status="confirmed",usage_kind="vat_deduction",usage_confirmation_id="usage",accounting_period="2026-10"))
    now=datetime.now(UTC)
    case={"external_case_id":"red","confirmation_id":"c","authority_revision":3,"status":"issued","our_side":True,"initiated_at":now.isoformat(),"authority_deadline":(now+timedelta(hours=72)).isoformat(),"blue_invoice_id":"blue","buyer_tax_id":"BUYER","seller_tax_id":"SELLER","amount":"100.00","tax":"10.00","red_invoice_number":"red","line_adjustments":{},"line_tax_adjustments":{}}
    await store.verify_invoice(owner,"blue",3,"issued")
    verify_job=await store.claim_compliance()
    assert verify_job is not None
    await result_for(store,verify_job,fresh_snapshot(3,verification_id="issued-v",red_letter_cases=[case],adjustments=[{"external_adjustment_id":"adjust","status":"required","reason":"转出补件","amount":"10.00","tax":"1.00"}]))
    await invoice(store,"red",is_red=True)
    await originals(store,"red")
    await store.verify_invoice(owner,"red",1,"red-v")
    red_job=await store.claim_compliance()
    assert red_job is not None
    await result_for(store,red_job,fresh_snapshot(verification_id="red-v"))
    red_case=(await store.list_red_letter_cases(tax,"e"))["items"][0]
    await store.link_red_letter_invoice(tax,red_case["id"],RedLetterInvoiceLink(expected_case_version=1,blue_invoice_id="blue",expected_blue_invoice_version=1,red_invoice_id="red",expected_red_invoice_version=1),"link")
    adjustment=(await store.list_tax_adjustments(tax,"e"))["items"][0]
    await store.record_tax_adjustment_evidence(tax,adjustment["id"],TaxAdjustmentEvidence(expected_version=1,reason="补件收到",evidence_ids=["original-blue"]),"material")
    archive=await archive_plan(store,tax)
    frozen=json.loads(await store.pool.fetchval("SELECT data FROM finance_compliance_plan WHERE id=$1",archive["id"]))["bindings"]["archive_snapshot"]
    artifacts={a["id"]:a for a in frozen["artifacts"]}
    assert "original_original-red" in artifacts
    assert "adjustment_material_original-blue" in artifacts
    assert frozen["snapshots"]["invoice_structure"]["red"]["fields"]
    history=frozen["snapshots"]["compliance_history"]
    assert any(d["action"] == "approve" and d["actor_id"] == auditor.user_id for d in history["decisions"])
    assert any(g["plan_id"] == plan["id"] for g in history["grants"])
    assert frozen["snapshots"]["tax_adjustments"]["materials"]
    assert all(p["data"]["action"] != "archive" for p in history["plans"])

"""合规事实应投影到现有发票读接口而保留原票面版本。"""
from __future__ import annotations

from typing import Any

from .test_f3_domain import configured, fresh_snapshot, invoice, result_for


async def test_get_invoice_projects_tax_confirmations_and_stale_verification(store: Any) -> None:
    """独立快照不能只在新接口可见，旧getInvoice也必须显示当前状态。"""
    owner,_,_ = await configured(store)
    await invoice(store)
    await store.verify_invoice(owner,"blue",1,"verify")
    job = await store.claim_compliance()
    await result_for(store,job,fresh_snapshot(usage_status="confirmed",usage_kind="vat_deduction",
        usage_confirmation_id="usage1",tax_entry_status="confirmed",tax_entry_confirmation_id="entry1",accounting_period="2026-10"))
    current = await store.get_invoice(owner,"blue")
    assert current["usage_status"] == "confirmed"
    assert current["tax_entry_status"] == "confirmed"
    assert current["version"] == 1
    await store.pool.execute("UPDATE finance_invoice_compliance SET valid_until=now()-interval '1 second'")
    assert (await store.get_invoice(owner,"blue"))["verification_status"] == "stale"


async def test_current_policy_shortens_old_passed_ttl_after_pending_refresh(store: Any) -> None:
    """新策略缩短期限不能被旧passed或后续pending观察时刻绕过。"""
    import json
    from datetime import UTC, datetime, timedelta

    from xuanpu_finance.compliance.models import CompliancePolicy

    owner,_,_ = await configured(store)
    await invoice(store)
    await store.verify_invoice(owner,"blue",1,"old")
    await result_for(store,await store.claim_compliance(),fresh_snapshot(
        observed_at=(datetime.now(UTC)-timedelta(hours=2)).isoformat()))
    version = (await store.get_invoice_compliance(owner,"blue"))["version"]
    await store.verify_invoice(owner,"blue",version,"pending")
    await result_for(store,await store.claim_compliance(),fresh_snapshot(2,verification_status="pending",verification_id=None))
    assert (await store.get_invoice(owner,"blue"))["verification_status"] == "passed"
    policy = json.loads(await store.pool.fetchval("SELECT data FROM finance_compliance_policy_revision"))
    policy.update(enabled=True,version=2,verification_max_age_hours=1)
    providers = [{**json.loads(row["data"]),"enabled":True} for row in await store.pool.fetch("SELECT data FROM finance_compliance_provider")]
    await store.configure_compliance([CompliancePolicy.model_validate(policy)],providers,simulation=True)
    assert (await store.get_invoice(owner,"blue"))["verification_status"] == "stale"


async def test_partial_red_does_not_freeze_unaffected_line(store: Any) -> None:
    """红冲已完整定位行A时，未受影响行B仍按真实剩余额度使用。"""
    import json

    from xuanpu_finance.storage.f3_projection import effective_line_limits

    await configured(store)
    await invoice(store)
    data = dict(amount="30.00",tax="3.00",line_adjustments={"lineA":"30.00"},line_tax_adjustments={"lineA":"3.00"})
    await store.pool.execute("INSERT INTO finance_red_letter_case(tenant_id,legal_entity_id,id,owner_id,invoice_id,external_case_id,authority_revision,status,data) VALUES('t','e','red','owner','blue','red',1,'issued',$1::jsonb)",json.dumps(data))
    async with store.pool.acquire() as conn:
        gross,tax = await effective_line_limits(conn,"t","e","blue","lineB",{"amount":"45.00","tax":"5.00"})
    assert str(gross) == "50.00" and str(tax) == "5.00"


async def test_human_tax_plan_exposes_derived_amount_and_tax(store: Any) -> None:
    """人工确认卡必须能看到实际推导金额和税额，不能只显示破折号。"""
    from .test_f3_guards import usage_plan, verified

    _,tax,_ = await verified(store)
    plan = await usage_plan(store,tax)
    assert plan["amount"] == "100.00" and plan["tax"] == "10.00"

"""真实PG上的独立F3查验、版本与恢复队列。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal

from .test_f3_models import tax_snapshot


async def configured(store: Any) -> tuple[FinancePrincipal, FinancePrincipal, FinancePrincipal]:
    """配置与申请人分离的经办和审批人，以及显式隔离渠道。"""
    from xuanpu_finance.compliance.models import CompliancePolicy

    await store.seed_company("t", "e", "公司", "BUYER")
    for user, role in [("owner", "collector"), ("tax", "reviewer"), ("audit", "approver")]:
        await store.grant("t", user, "e", role)
    policy = CompliancePolicy(tenant_id="t", legal_entity_id="e", version=1, enabled=True, tax_enabled=True, archive_enabled=True, tax_provider="tax", tax_configuration_version=1, archive_provider="archive", archive_configuration_version=1, tax_executor_user_ids=["tax"], archive_executor_user_ids=["tax"], tax_steps=[{"id": "s", "name": "税务审核", "approver_user_id": "audit"}], archive_steps=[{"id": "a", "name": "归档审核", "approver_user_id": "audit"}], open_periods=["2026-10"], supported_invoice_profiles=["urn:xuanpu:finance:synthetic:v1"], archive_profile_id="simulation-archive-v1", archive_profile_version=1, required_lock_mode="SIMULATION")
    providers = [{"tenant_id": "t", "legal_entity_id": "e", "provider": kind, "configuration_version": 1, "adapter_version": "http-v1", "kind": kind, "base_url": "http://127.0.0.1:1", "simulation": True, "enabled": True, "supports_idempotency": True, "supports_query": True, "supports_state_revision": True, "supports_object_versions": True, "supports_retention_readback": True} for kind in ["tax", "archive"]]
    await store.configure_compliance([policy], providers, simulation=True)
    return FinancePrincipal("t", "owner"), FinancePrincipal("t", "tax", "human"), FinancePrincipal("t", "audit", "human")


async def invoice(store: Any, id: str = "blue", *, is_red: bool = False, simulation: bool = True) -> dict[str, Any]:
    """写入隔离票据，保留原始content版本。"""
    data = {"id": id, "version": 1, "legal_entity_id": "e", "parser_profile": "urn:xuanpu:finance:synthetic:v1", "seller_tax_id": "SELLER", "buyer_tax_id": "BUYER", "number": id, "issue_date": "2026-10-20", "net": "90.00" if not is_red else "-90.00", "gross": "100.00" if not is_red else "-100.00", "tax": "10.00" if not is_red else "-10.00", "is_red": is_red, "ticket_status": "normal", "verification_status": "not_checked"}
    await store.pool.execute("INSERT INTO finance_invoice(tenant_id,legal_entity_id,id,owner_id,seller_tax_id,number,issue_date,data,canonical_digest,source_simulation) VALUES('t','e',$1,'owner','SELLER',$1,'2026-10-20',$2::jsonb,$3,$4)", id, json.dumps(data), "a" * 64, simulation)
    return data


def fresh_snapshot(revision: int = 1, **changes: Any) -> dict[str, Any]:
    """用当前时间产生未过期的权威快照。"""
    now = datetime.now(UTC)
    return {**tax_snapshot(), "authority_revision": revision, "observed_at": now.isoformat(), "valid_until": (now + timedelta(hours=12)).isoformat(), **changes}


async def result_for(store: Any, job: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """通过实际claim/prepare/result边界应用渠道事实。"""
    request = await store.prepare_compliance_dispatch(job)
    result = {**{k: v for k, v in request.items() if k != "payload"}, "status": "succeeded", "evidence": [{"id": "evidence", "sha256": "b" * 64, "media_type": "application/json", "object_ref": "fixed-provider-object", "size": 2}], "outcome": snapshot}
    raw = json.dumps(result).encode()
    await store.record_compliance_result(job, result, raw)
    await store.release_compliance(job)


async def test_f3_tables_and_verification_are_independent_of_expense_parent(store: Any) -> None:
    """无任何报销父单据也能查验，且内容版本不受合规事件覆盖。"""
    assert hasattr(store, "configure_compliance"), "缺少F3独立领域接口"
    owner, _, _ = await configured(store)
    original = await invoice(store)
    operation = await store.verify_invoice(owner, "blue", 1, "verify")
    assert operation["id"].startswith("f3op_")
    assert operation["status"] == "accepted"
    job = await store.claim_compliance()
    assert job is not None and job["mode"] == "submit"
    await result_for(store, job, fresh_snapshot())
    state = await store.get_invoice_compliance(owner, "blue")
    assert state["version"] == 2
    assert state["verification_status"] == "passed"
    assert json.loads(await store.pool.fetchval("SELECT data FROM finance_invoice WHERE id='blue'")) == original
    assert await store.pool.fetchval("SELECT count(*) FROM finance_expense_claim") == 0
    assert (await store.list_invoice_verifications(owner, "blue"))["total"] == 1


async def test_default_closed_and_content_source_cannot_be_upgraded_to_simulation(store: Any) -> None:
    """默认关闭；旧正式原件不能通过当前隔离配置升级为模拟票。"""
    owner, _, _ = await configured(store)
    await invoice(store, simulation=False)
    with pytest.raises(FinanceError, match="环境"):
        await store.verify_invoice(owner, "blue", 1, "verify")


async def test_stale_revision_preserves_raw_and_does_not_roll_back_state(store: Any) -> None:
    """较旧权威回执原字节保全且不能回退有效通过状态。"""
    owner, _, _ = await configured(store)
    await invoice(store)
    for revision, status in [(5, "passed"), (4, "failed")]:
        await store.verify_invoice(owner, "blue", (await store.get_invoice_compliance(owner, "blue"))["version"], f"v{revision}")
        job = await store.claim_compliance()
        assert job is not None
        await result_for(store, job, fresh_snapshot(revision, verification_status=status))
    state = await store.get_invoice_compliance(owner, "blue")
    assert state["authority_revision"] == 5
    assert state["verification_status"] == "passed"
    assert await store.pool.fetchval("SELECT count(*) FROM finance_compliance_receipt") == 2


async def test_unknown_result_is_never_resubmitted(store: Any) -> None:
    """发出后失联与过期租约均只恢复原请求GET。"""
    owner, _, _ = await configured(store)
    await invoice(store)
    await store.verify_invoice(owner, "blue", 1, "v")
    job = await store.claim_compliance()
    assert job is not None
    request = await store.prepare_compliance_dispatch(job)
    await store.defer_compliance(job, "TIMEOUT", uncertain=True)
    await store.release_compliance(job)
    await store.pool.execute("UPDATE finance_compliance_operation SET next_query_at=now()")
    query = await store.claim_compliance(allow_dispatch=False)
    assert query is not None and query["mode"] == "query"
    assert query["request"] == request


async def test_provider_epoch_has_own_revision_sequence_and_old_inflight_source_cannot_overwrite(store: Any) -> None:
    """切换供应商后B1可取代A100，但旧A101只保全原文和未应用历史。"""
    from xuanpu_finance.compliance.models import CompliancePolicy

    owner,_,_=await configured(store)
    await invoice(store)
    await store.verify_invoice(owner,"blue",1,"a100")
    first=await store.claim_compliance()
    assert first is not None
    await result_for(store,first,fresh_snapshot(100,verification_id="a100"))
    await store.verify_invoice(owner,"blue",2,"a101")
    old=await store.claim_compliance()
    assert old is not None
    old_request=await store.prepare_compliance_dispatch(old)
    policy_data=json.loads(await store.pool.fetchval("SELECT data FROM finance_compliance_policy_revision"))
    policy=CompliancePolicy.model_validate({**policy_data,"version":2,"enabled":True,"tax_provider":"tax-b"})
    providers=[{**json.loads(row["data"]),"enabled":True} for row in await store.pool.fetch("SELECT data FROM finance_compliance_provider")]
    providers.append({**next(p for p in providers if p["kind"] == "tax"),"provider":"tax-b"})
    await store.configure_compliance([policy],providers,simulation=True)
    await store.verify_invoice(owner,"blue",2,"b1")
    new=await store.claim_compliance()
    assert new is not None and new["provider"] == "tax-b"
    await result_for(store,new,fresh_snapshot(1,verification_id="b1"))
    state=await store.get_invoice_compliance(owner,"blue")
    assert state["authority_revision"] == 1
    assert state["provider"] == "tax-b"
    result={**{k:v for k,v in old_request.items() if k != "payload"},"status":"succeeded","evidence":[{"id":"p","sha256":"b"*64,"media_type":"application/json","object_ref":"fixed"}],"outcome":fresh_snapshot(101,verification_id="a101",verification_status="failed")}
    await store.record_compliance_result(old,result,json.dumps(result).encode())
    final=await store.get_invoice_compliance(owner,"blue")
    assert final["provider"] == "tax-b"
    assert final["authority_revision"] == 1
    assert final["verification_status"] == "passed"
    assert await store.pool.fetchval("SELECT count(*) FROM finance_compliance_receipt") == 3


async def test_compliance_simulation_marker_applies_to_new_business_records_without_f2_provider(store: Any) -> None:
    """独立F3配置也必须给新F1单据正确隔离来源，而不依赖F2渠道存在。"""
    from .test_expense_domain import claim, setup

    await configured(store)
    owner,_=await setup(store)
    record=await claim(store,owner,"20.00")
    assert record["simulation"] is True


async def test_invalid_jsonb_text_cannot_erase_raw_provider_receipt(store: Any) -> None:
    """DTO无效字符也必须先保全原始bytes，再将动作留为未知。"""
    owner,_,_=await configured(store)
    await invoice(store)
    await store.verify_invoice(owner,"blue",1,"bad-text")
    job=await store.claim_compliance()
    assert job is not None
    await result_for(store,job,fresh_snapshot(verification_id="bad\x00id"))
    assert await store.pool.fetchval("SELECT count(*) FROM finance_compliance_receipt") == 1
    assert b"bad\\u0000id" in await store.pool.fetchval("SELECT raw FROM finance_compliance_receipt")
    assert (await store.get_invoice_compliance(owner,"blue"))["version"] == 1
    assert (await store.get_operation(owner,job["id"]))["status"] == "outcome_unknown"

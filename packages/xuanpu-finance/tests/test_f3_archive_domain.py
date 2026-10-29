"""封存事实领域接收边界、发送后变化及后续审计追加。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from xuanpu_finance.compliance.models import ComplianceActionPreview
from xuanpu_finance.expenses.models import ActionExecution

from .test_f3_domain import fresh_snapshot, result_for
from .test_f3_guards import approve, verified

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal


async def originals(store: Any, id: str = "blue", *, structure: bool = True) -> None:
    """记录实际已接收原件的领域事实，不替代管线实际bytes测试。"""
    raw = b"<synthetic-invoice/>"
    await store.pool.execute("INSERT INTO finance_operation(tenant_id,legal_entity_id,id,owner_id,idempotency_key,request_hash,trace_id,source_simulation) VALUES('t','e',$1,'owner',$1,'hash','trace',true)", "import-" + id)
    await store.pool.execute("INSERT INTO finance_item(tenant_id,legal_entity_id,operation_id,position,file_ref,state,object_key,actual_sha256,content_type,size_bytes) VALUES('t','e',$1,0,'{}','parsed',$2,$3,'application/xml',$4)", "import-"+id, "encrypted/"+id, hashlib.sha256(raw).hexdigest(), len(raw))
    await store.pool.execute("INSERT INTO finance_receipt VALUES('t','e',$1,'owner',$2,0,$3,$4,$5::jsonb)", "original-"+id,"import-"+id,"encrypted/"+id,hashlib.sha256(raw).hexdigest(), json.dumps({"revision":1}))
    await store.pool.execute("INSERT INTO finance_invoice_receipt VALUES('t','e',$1,$2)", id,"original-"+id)
    if structure:
        await store.pool.execute("INSERT INTO finance_invoice_line VALUES('t','e',$1,0,$2::jsonb)",id,json.dumps({"id":id+"-line","amount":"90.00","tax":"10.00"}))
        await store.pool.execute("INSERT INTO finance_invoice_field VALUES('t','e',$1,0,$2::jsonb)",id,json.dumps({"name":"gross","value":"100.00","source":"synthetic"}))


async def archive_plan(store: Any, tax: FinancePrincipal, *, parent: str | None = None, events: list[str] | None = None) -> dict[str, Any]:
    """封存计划绑定当前合规版本和明确补充来源。"""
    version = (await store.get_invoice_compliance(tax, "blue"))["version"]
    parameters = {"parent_manifest_hash":parent,"source_event_ids":events} if parent else {}
    plan = await store.preview_finance_action(tax, ComplianceActionPreview(action="archive",target_type="invoice",target_id="blue",expected_version=version,parameters=parameters),"archive-"+str(version)+str(parent))
    return await store.get_action_plan(tax,plan["id"])


async def accept_archive(store: Any, tax: FinancePrincipal, auditor: FinancePrincipal) -> tuple[dict[str, Any],dict[str, Any]]:
    """经过实际人工批准和发送冻结取得领域任务。"""
    plan = await archive_plan(store,tax)
    await approve(store,auditor,plan)
    await store.execute_finance_action(tax,ActionExecution(plan_id=plan["id"],plan_digest=plan["digest"]),"archive-execute")
    job = await store.claim_compliance()
    assert job is not None
    request = await store.prepare_compliance_dispatch(job)
    return job,request


def outcome_for(request: dict[str, Any]) -> dict[str, Any]:
    """构造严格封存回读事实以验证领域二次清册核验。"""
    snapshot = request["payload"]["archive_snapshot"]
    sealed_at = datetime.now(UTC)
    until = (max(datetime.fromisoformat(snapshot["retention"]["anchor_at"]), sealed_at)+timedelta(days=snapshot["retention"]["days"])).isoformat()
    members = [{**{key:artifact[key] for key in ("id","path","sha256","size","media_type")},"version_id":"version-"+artifact["id"],"lock_mode":"SIMULATION","retained_until":until,"legal_hold":False} for artifact in snapshot["artifacts"]]
    members.extend({"id":id,"path":path,"sha256":digest,"size":12,"media_type":"application/json","version_id":"version-"+id,"lock_mode":"SIMULATION","retained_until":until,"legal_hold":False} for id,path,digest in [("manifest","manifest.json","c"*64),("seal","seal-attestation.json","d"*64)])
    return {"kind":"archive","sealed_at":sealed_at.isoformat(),"package_id":"pack","manifest_hash":"c"*64,"manifest_member_id":"manifest","seal_member_id":"seal","standard_profile":"simulation-archive-v1","retention_class":snapshot["retention"]["retention_class"],"retained_until":until,"legal_hold":False,"members":members,"four_qualities":[{"name":name,"status":"passed","detector_version":"simulation-v1","checked_at":datetime.now(UTC).isoformat(),"input_hash":snapshot["source_snapshot_hash"],"evidence":["domain-boundary-fixture"]} for name in ("authenticity","integrity","usability","security")],"parent_manifest_hash":snapshot["parent_manifest_hash"],"source_snapshot_hash":snapshot["source_snapshot_hash"],"readback_verified":True}


async def save_archive_result(store: Any,job: dict[str, Any],request: dict[str, Any],outcome: dict[str, Any]) -> None:
    """在队列租约下保存模拟管线提供的严格事实。"""
    result={**{k:v for k,v in request.items() if k != "payload"},"status":"succeeded","evidence":[{"id":"proof","sha256":"f"*64,"media_type":"application/json","object_ref":"archive-object"}],"outcome":outcome}
    await store.record_compliance_result(job,result,json.dumps(result).encode())
    await store.release_compliance(job)


async def test_archive_blocks_missing_structured_sources(store: Any) -> None:
    """真实原件不能替代缺失的解析明细和字段来源。"""
    _,tax,_=await verified(store)
    await originals(store,structure=False)
    plan=await archive_plan(store,tax)
    assert any("ARCHIVE_STRUCTURED_REQUIRED" in value for value in plan["blockers"])


async def test_live_change_after_dispatch_preserves_original_archive_and_requires_supplement(store: Any) -> None:
    """发出后新税务事实不会使已真实封存原包丢失。"""
    owner,tax,auditor=await verified(store)
    await originals(store)
    job,request=await accept_archive(store,tax,auditor)
    assert request["payload"]["archive_snapshot"]["content_version"] == 1
    assert request["payload"]["archive_snapshot"]["compliance_version"] == 2
    await store.verify_invoice(owner,"blue",2,"new-fact")
    tax_job=await store.claim_compliance()
    assert tax_job is not None
    await result_for(store,tax_job,fresh_snapshot(2,verification_id="new"))
    await save_archive_result(store,job,request,outcome_for(request))
    records=await store.list_archives(tax,"e")
    assert records["total"] == 1
    record=records["items"][0]
    assert record["supplement_required"] is True
    assert record["snapshot"]["compliance_version"] == 2
    assert record["supplement_event_ids"]
    assert "object_key" not in json.dumps(record)
    supplement=await archive_plan(store,tax,parent=record["manifest_hash"],events=record["supplement_event_ids"])
    assert all("SOURCE_INVALID" not in blocker for blocker in supplement["blockers"])
    await approve(store,auditor,supplement)
    await store.execute_finance_action(tax,ActionExecution(plan_id=supplement["id"],plan_digest=supplement["digest"]),"supplement-execute")
    supplement_job=await store.claim_compliance()
    assert supplement_job is not None
    supplement_request=await store.prepare_compliance_dispatch(supplement_job)
    outcome=outcome_for(supplement_request)
    outcome["manifest_hash"]="e"*64
    outcome["members"][-2]["sha256"]="e"*64
    await save_archive_result(store,supplement_job,supplement_request,outcome)
    completed=await store.get_archive(tax,record["id"])
    assert completed["supplement_event_ids"] == []
    assert completed["supplement_required"] is False


async def test_archive_member_scope_mismatch_preserves_raw_without_materializing_false_archive(store: Any) -> None:
    """渠道称成功但清册摘要不符时只保留回执与未知状态。"""
    _,tax,auditor=await verified(store)
    await originals(store)
    job,request=await accept_archive(store,tax,auditor)
    outcome=outcome_for(request)
    outcome["members"][0]["sha256"]="0"*64
    await save_archive_result(store,job,request,outcome)
    assert (await store.list_archives(tax,"e"))["total"] == 0
    operation=await store.get_operation(tax,job["id"])
    assert operation["status"] == "outcome_unknown"
    assert operation["last_error"] == "ARCHIVE_MEMBER_MISMATCH"


async def test_failed_archive_audit_appends_health_without_modifying_original_snapshot(store: Any) -> None:
    """审计读回失败保留原封存事实并追加新的健康记录。"""
    _,tax,auditor=await verified(store)
    await originals(store)
    job,request=await accept_archive(store,tax,auditor)
    await save_archive_result(store,job,request,outcome_for(request))
    archived=(await store.list_archives(tax,"e"))["items"][0]
    await store.audit_archive(tax,archived["id"],1,"audit")
    audit_job=await store.claim_compliance()
    assert audit_job is not None
    audit_request=await store.prepare_compliance_dispatch(audit_job)
    response={**{k:v for k,v in audit_request.items() if k != "payload"},"status":"unknown","evidence":[],"failure_code":"READBACK_FAILED","readback_evidence":[{"object_base64":"YnJva2Vu","code":"CORRUPT"}]}
    await store.record_compliance_result(audit_job,response,b"original-success-receipt")
    audits=await store.get_archive_audits(tax,archived["id"])
    assert audits["total"] == 1
    assert audits["items"][0]["data"]["status"] == "unavailable"
    current=await store.get_archive(tax,archived["id"])
    assert current["snapshot"] == archived["snapshot"]
    assert current["last_audit_status"] == "unavailable"


async def test_new_related_financial_event_requires_archive_supplement_without_compliance_version_change(store: Any) -> None:
    """付款或记账的新事实不能被相同合规max版本漏过。"""
    from .test_expense_domain import allocate, claim, setup

    owner,tax,auditor=await verified(store)
    await originals(store)
    job,request=await accept_archive(store,tax,auditor)
    await save_archive_result(store,job,request,outcome_for(request))
    archived=(await store.list_archives(tax,"e"))["items"][0]
    assert archived["supplement_required"] is False
    await setup(store)
    doc=await claim(store,owner,amount="20.00")
    await allocate(store,owner,doc,id="blue",amount="20.00")
    await store.pool.execute("INSERT INTO finance_event(tenant_id,legal_entity_id,aggregate_id,kind,data) VALUES('t','e',$1,'settlement.succeeded','{}')",doc["id"])
    current=await store.get_archive(tax,archived["id"])
    assert current["supplement_required"] is True
    assert current["supplement_event_ids"]
    assert current["snapshot"] == archived["snapshot"]


async def test_domain_rejects_manifest_member_hash_and_quality_scope_forgery(store: Any) -> None:
    """存储二次验证不能只信readback_verified标志和成员锁回显。"""
    _,tax,auditor=await verified(store)
    await originals(store)
    job,request=await accept_archive(store,tax,auditor)
    outcome=outcome_for(request)
    outcome["members"][-2]["sha256"]="0"*64
    outcome["four_qualities"][0]["input_hash"]="0"*64
    await save_archive_result(store,job,request,outcome)
    assert (await store.list_archives(tax,"e"))["total"] == 0
    assert (await store.get_operation(tax,job["id"]))["status"] == "outcome_unknown"

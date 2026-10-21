"""真实独立PG、签名API、回环HTTP税务和档案端到端闭环。"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.compliance.archive import ArchivePipeline
from xuanpu_finance.compliance.evidence import EvidenceFetcher
from xuanpu_finance.compliance.models import CompliancePolicy
from xuanpu_finance.compliance.protocol import ComplianceProviderConfig
from xuanpu_finance.compliance.provider import ComplianceRegistry
from xuanpu_finance.compliance.queue import ComplianceQueueAdapter
from xuanpu_finance.invoices.parser import PROFILE
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.settlement.worker import SettlementWorker

from .f3_contract_checks import PublishedResponses
from .f3_http_service import ComplianceService
from .test_f0_e2e import SignedClient, app_for
from .test_f3_domain import configured
from .test_parser import SAMPLE
from .test_worker import Gateway, ref

if TYPE_CHECKING:
    from pathlib import Path


async def configure_http(store: Any, tmp_path: Path, base_url: str) -> list[ComplianceProviderConfig]:
    """新版本配置真实回环origin；不覆盖已经持久的旧渠道版本。"""
    policy = json.loads(await store.pool.fetchval("SELECT data FROM finance_compliance_policy_revision"))
    policy.update(version=2,enabled=True,supported_invoice_profiles=[PROFILE],
                  tax_configuration_version=2,archive_configuration_version=2)
    configs = [ComplianceProviderConfig.model_validate({**json.loads(row["data"]),
        "enabled":True,"configuration_version":2,"base_url":base_url,
        "token_file":tmp_path/"token","callback_secret_file":tmp_path/"callback", "callback_key_id":"test"})
        for row in await store.pool.fetch("SELECT data FROM finance_compliance_provider ORDER BY provider")]
    await store.configure_compliance([CompliancePolicy.model_validate(policy)],
        [config.public_descriptor() for config in configs],simulation=True)
    return configs


async def approve_plan(call: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """真实签名人工决定走统一API，不能直接插批准记录。"""
    original = await call("GET",f"/v1/finance/action-plans/{plan['id']}",user="audit")
    step = original["workflow"]["current_step_id"]
    review = await call("POST","/v1/finance/action-previews",user="audit",body=dict(
        action="approve",target_type="action_plan",target_id=plan["id"],expected_version=1,
        parameters={"workflow_step_id":step}),idem="review-"+plan["id"])
    assert review["executable"],review["blockers"]
    await call("POST","/v1/finance/action-executions",user="audit",
        body={"plan_id":review["id"],"plan_digest":review["digest"]},idem="decide-"+plan["id"])
    return await call("GET",f"/v1/finance/action-plans/{plan['id']}",user="tax")


async def test_real_import_tax_hitl_archive_and_readonly_audit(store: Any,tmp_path: Path) -> None:
    """实际原件→查验→专用审批→税务确认→归档上传/读回→只读审计。"""
    owner,tax,_ = await configured(store)
    service = ComplianceService()
    server = await asyncio.start_server(service.handle,"127.0.0.1",0)
    port = server.sockets[0].getsockname()[1]
    configs = await configure_http(store,tmp_path,f"http://127.0.0.1:{port}")
    objects,gateway,key = EncryptedLocalObjects(tmp_path/"objects",b"k"*32),Gateway({"invoice":SAMPLE}),Ed25519PrivateKey.generate()
    app = app_for(store,objects,gateway,key)
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=5) as outbound, httpx.AsyncClient(
            transport=httpx.ASGITransport(app),base_url="https://finance") as client:
            registry = ComplianceRegistry(configs,outbound,tokens={(c.provider,2):"token" for c in configs},
                callback_secrets={(c.provider,2):"s"*32 for c in configs})
            pipeline = ArchivePipeline(registry,objects,EvidenceFetcher({"compliance":registry}))
            registry.archive = pipeline
            worker = SettlementWorker(ComplianceQueueAdapter(store,pipeline),registry,gateway)
            signed = SignedClient(client,key)
            contract = PublishedResponses()

            async def call(method: str,path: str,*,user: str="owner",**kwargs: Any) -> dict[str,Any]:
                """保留真实Ed25519签名验证和领域权限。"""
                response = await signed.call(method,path,tenant="t",user=user,origin="human",**kwargs)
                assert response.is_success,response.text
                value = response.json()
                contract.validate(method,path,response.status_code,value)
                return value

            await call("POST","/v1/finance/invoice-imports",body={"legal_entity_id":"e","files":[ref("invoice",SAMPLE).model_dump()]},idem="import")
            await app.state.worker.run_once()
            invoice = (await call("GET","/v1/finance/invoices",params={"legal_entity_id":"e"}))["items"][0]
            id = invoice["id"]
            service.malformed_next = True
            op = await call("POST",f"/v1/finance/invoices/{id}/verifications",body={"expected_version":1},idem="verify")
            assert await worker.run_once()
            unknown = await call("GET",f"/v1/finance/operations/{op['id']}")
            assert unknown["status"] == "outcome_unknown"
            await call("POST",f"/v1/finance/operations/{op['id']}/reconcile",body={"expected_version":unknown["version"]},idem="recover")
            assert await worker.run_once(allow_dispatch=False)
            assert (await call("GET",f"/v1/finance/operations/{op['id']}"))["status"] == "succeeded"
            state = await call("GET",f"/v1/finance/invoices/{id}/compliance")
            assert state["verification_status"] == "passed" and state["version"] == 2
            await tax_and_archive(call,worker,id,state,service)
            await additional_read_contracts(call,id)
            await red_and_adjustment_contracts(call,worker,app,gateway,service,id)
            await supplement_and_zip_closure(call,worker,service,gateway,id)
            assert len(contract.checked) == 20,contract.checked

    finally:
        server.close()
        await server.wait_closed()


async def tax_and_archive(call: Any,worker: Any,id: str,state: Any,service: Any) -> None:
    """真实人工链之后分别执行税务与归档，不以预检取得执行资格。"""
    for action,parameters in [("confirm_usage",{"accounting_period":"2026-10"}),("archive",{})]:
        state = await call("GET",f"/v1/finance/invoices/{id}/compliance")
        plan = await call("POST","/v1/finance/action-previews",user="tax",body=dict(action=action,
            target_type="invoice",target_id=id,expected_version=state["version"],parameters=parameters),idem=action)
        assert not plan["executable"]
        plan = await approve_plan(call,plan)
        assert plan["executable"],plan["blockers"]
        op = await call("POST","/v1/finance/action-executions",user="tax",
            body={"plan_id":plan["id"],"plan_digest":plan["digest"]},idem="execute-"+action)
        assert await worker.run_once()
        completed = await call("GET",f"/v1/finance/operations/{op['id']}",user="tax")
        assert completed["status"] == "succeeded",completed
    archives = await call("GET","/v1/finance/archives",user="tax",params={"legal_entity_id":"e"})
    archive = archives["items"][0]
    assert archive["data"]["readback_verified"]
    before = sum(method=="POST" for method,_ in service.seen)
    audit = await call("POST",f"/v1/finance/archives/{archive['id']}/audits",user="tax",
        body={"expected_version":archive["version"]},idem="audit")
    assert await worker.run_once()
    assert (await call("GET",f"/v1/finance/operations/{audit['id']}",user="tax"))["status"] == "succeeded"
    assert sum(method=="POST" for method,_ in service.seen) == before


async def additional_read_contracts(call: Any,id: str) -> None:
    """覆盖真实查验、确认、计划和档案各独立恢复接口。"""
    await call("GET",f"/v1/finance/invoices/{id}/verifications")
    for operation in ("compliance-options","compliance-action-plans","tax-confirmations","red-letter-cases","tax-adjustments"):
        await call("GET","/v1/finance/"+operation,user="tax",params={"legal_entity_id":"e"})
    archives = await call("GET","/v1/finance/archives",user="tax",params={"legal_entity_id":"e"})
    archive = archives["items"][0]
    for suffix in ("","/manifest","/tests","/evidence","/supplements","/audits"):
        await call("GET",f"/v1/finance/archives/{archive['id']}"+suffix,user="tax")


async def red_and_adjustment_contracts(call: Any,worker: Any,app: Any,gateway: Any,service: Any,id: str) -> None:
    """红字权威事实、独立红票原件和人工补件均走实际签名API。"""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    line = (await call("GET",f"/v1/finance/invoices/{id}/lines"))["items"][0]
    case = dict(external_case_id="external-red1",confirmation_id="confirmation1",authority_revision=service.revision+1,
        status="awaiting_confirmation",our_side=True,initiated_at=now.isoformat(),authority_deadline=(now+timedelta(hours=71)).isoformat(),
        blue_invoice_id=id,buyer_tax_id="BUYER",seller_tax_id="SELLER",amount="106.00",tax="6.00",
        red_invoice_number="SYN-RED-001",line_adjustments={line["id"]:"106.00"},line_tax_adjustments={line["id"]:"6.00"})
    service.red_cases = [case]
    service.adjustments = [dict(external_adjustment_id="adjustment1",status="required",reason="红冲税务调整",
                               amount="106.00",tax="6.00",accounting_period="2026-10")]
    state = await call("GET",f"/v1/finance/invoices/{id}/compliance")
    await call("POST",f"/v1/finance/invoices/{id}/verifications",body={"expected_version":state["version"]},idem="read-case")
    assert await worker.run_once()
    cases = await call("GET","/v1/finance/red-letter-cases",user="tax",params={"legal_entity_id":"e"})
    red = await call("GET",f"/v1/finance/red-letter-cases/{cases['items'][0]['id']}",user="tax")
    plan = await call("POST","/v1/finance/action-previews",user="tax",body=dict(action="confirm_red_letter",
        target_type="red_letter_case",target_id=red["id"],expected_version=red["version"],parameters={"confirmation_id":"confirmation1"}),idem="confirm-red")
    plan = await approve_plan(call,plan)
    assert plan["executable"],plan["blockers"]
    await call("POST","/v1/finance/action-executions",user="tax",body={"plan_id":plan["id"],"plan_digest":plan["digest"]},idem="execute-red")
    assert await worker.run_once()
    service.red_cases = [{**case,"status":"issued","authority_revision":service.revision+1}]
    red = await call("GET",f"/v1/finance/red-letter-cases/{red['id']}",user="tax")
    await call("POST",f"/v1/finance/red-letter-cases/{red['id']}/refresh",user="tax",body={"expected_version":red["version"]},idem="refresh-red")
    assert await worker.run_once()
    raw = SAMPLE.replace(b"SYN-001",b"SYN-RED-001").replace(b">100.00<",b">-100.00<").replace(b">6.00<",b">-6.00<").replace(b">106.00<",b">-106.00<")
    gateway.payloads["red"] = raw
    await call("POST","/v1/finance/invoice-imports",body={"legal_entity_id":"e","files":[ref("red",raw).model_dump()]},idem="import-red")
    assert await app.state.worker.run_once()
    invoices = await call("GET","/v1/finance/invoices",params={"legal_entity_id":"e"})
    red_invoice = next(i for i in invoices["items"] if i["is_red"])
    await call("POST",f"/v1/finance/invoices/{red_invoice['id']}/verifications",body={"expected_version":1},idem="verify-red")
    assert await worker.run_once()
    red = await call("GET",f"/v1/finance/red-letter-cases/{red['id']}",user="tax")
    await call("POST",f"/v1/finance/red-letter-cases/{red['id']}/invoices",user="tax",body=dict(
        expected_case_version=red["version"],blue_invoice_id=id,expected_blue_invoice_version=1,
        red_invoice_id=red_invoice["id"],expected_red_invoice_version=1),idem="link-red")
    adjustments = await call("GET","/v1/finance/tax-adjustments",user="tax",params={"legal_entity_id":"e"})
    adjustment = adjustments["items"][0]
    evidence = (await call("GET",f"/v1/finance/invoices/{id}/evidence"))["evidence"][0]
    result = await call("POST",f"/v1/finance/tax-adjustments/{adjustment['id']}/evidence",user="tax",body=dict(
        expected_version=adjustment["version"],reason="附原始票据材料",evidence_ids=[evidence["id"]]),idem="adjustment-material")
    assert result["material_status"] == "received" and result["declaration_status"] == "not_checked"


async def supplement_and_zip_closure(call: Any,worker: Any,service: Any,gateway: Any,id: str) -> None:
    """检查新增红票、补件和实际人工决定进入真实ZIP并按父包补充封存。"""
    archives = await call("GET","/v1/finance/archives",user="tax",params={"legal_entity_id":"e"})
    parent = await call("GET",f"/v1/finance/archives/{archives['items'][0]['id']}",user="tax")
    assert parent["supplement_required"] and parent["supplement_event_ids"]
    state = await call("GET",f"/v1/finance/invoices/{id}/compliance",user="tax")
    plan = await call("POST","/v1/finance/action-previews",user="tax",body=dict(action="archive",target_type="invoice",target_id=id,
        expected_version=state["version"],parameters={"parent_manifest_hash":parent["manifest_hash"],"source_event_ids":parent["supplement_event_ids"]}),idem="supplement")
    plan = await approve_plan(call,plan)
    assert plan["executable"],plan["blockers"]
    op = await call("POST","/v1/finance/action-executions",user="tax",body={"plan_id":plan["id"],"plan_digest":plan["digest"]},idem="execute-supplement")
    assert await worker.run_once()
    result = await call("GET",f"/v1/finance/operations/{op['id']}",user="tax")
    assert result["status"] == "succeeded",result
    child_service = service.archives[result["external_request_id"]]
    assert gateway.payloads["red"] in child_service.contents.values()
    assert b'{"status":' in child_service.contents.values()
    materials = [child_service.contents[m["id"]] for m in child_service.members if m["path"].startswith("adjustment-materials/")]
    assert SAMPLE in materials
    history_member = next(m for m in child_service.members if m["path"].endswith("compliance-history.json"))
    history = json.loads(child_service.contents[history_member["id"]])["data"]
    assert history and "audit" in json.dumps(history) and "approve" in json.dumps(history)
    restored_parent = await call("GET",f"/v1/finance/archives/{parent['id']}",user="tax")
    assert restored_parent["manifest_hash"] == parent["manifest_hash"]
    assert restored_parent["snapshot"] == parent["snapshot"]
    assert not restored_parent["supplement_required"]

"""无模型时实际cookie与CSRF仍可完成税务专用人工批准。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.compliance.models import CompliancePolicy
from xuanpu_finance.compliance.protocol import ComplianceProviderConfig
from xuanpu_finance.compliance.provider import ComplianceRegistry
from xuanpu_finance.compliance.queue import ComplianceQueueAdapter
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.models import FileRef, FinancePrincipal
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.settlement.provider import RESULT_SCOPE
from xuanpu_finance.settlement.worker import SettlementWorker
from xuanpu_server.api import create_app
from xuanpu_server.manual_connectors import ManualConnectorService
from xuanpu_server.runtime.call_context import HumanContextSigner

from .test_finance_gateway_e2e import (
    F2CookieCalls,
    VerifiedFinanceTransport,
    f2_no_model,
    f2_people_and_expenses,
    f2_signer_config,
    finance_app,
)
from .test_finance_gateway_e2e import (
    finance_database as _finance_database,
)

if TYPE_CHECKING:
    from pathlib import Path

finance_database = _finance_database

RAW = b'<Invoice xmlns="urn:xuanpu:finance:synthetic:v1"><Number>SYN-COOKIE</Number><IssueDate>2026-10-21</IssueDate><SellerName>Seller</SellerName><SellerTaxId>SELLER</SellerTaxId><BuyerName>Buyer</BuyerName><BuyerTaxId>BUYER</BuyerTaxId><Currency>CNY</Currency><Amount>100.00</Amount><TaxAmount>6.00</TaxAmount><Total>106.00</Total><Lines><Line><Name>Service</Name><Amount>100.00</Amount><TaxRate>0.06</TaxRate><TaxAmount>6.00</TaxAmount></Line></Lines></Invoice>'


class OriginalGateway:
    """测试只提供实际收件原字节；后台身份另走真实平台接口。"""

    async def active(self, who: FinancePrincipal) -> bool:
        """仅用于隔离原件准备，不用于证明真实账号资格。"""
        return True

    async def download(self, who: FinancePrincipal, file: FileRef) -> tuple[bytes,str,str]:
        """实际XML进入独立加密库及解析流程。"""
        return RAW,"application/xml","cookie.xml"


def tax_response(request: httpx.Request) -> httpx.Response:
    """显式隔离权威快照，HTTP200本身不表示完成。"""
    import json

    body = json.loads(request.content)
    now = datetime.now(UTC)
    snapshot = dict(kind="tax",authority_revision=1 if body["action"]=="verify_invoice" else 2,
        observed_at=now.isoformat(),valid_until=(now+timedelta(hours=1)).isoformat(),verification_id="v1",
        verification_status="passed",invoice_status="normal",usage_status="unconfirmed",tax_entry_status="unconfirmed",red_letter_cases=[],adjustments=[])
    if body["action"]=="confirm_usage":
        snapshot.update(usage_status="confirmed",usage_kind="vat_deduction",usage_confirmation_id="usage1",accounting_period="2026-10")
    return httpx.Response(200,json={**{k:body[k] for k in RESULT_SCOPE},"status":"succeeded","outcome":snapshot,
        "evidence":[dict(id="tax-receipt",sha256="a"*64,media_type="application/json",object_ref="simulation-only")]})


async def prepare_finance(store: Any,people: Any,scope: Any,step: Any,tmp_path: Path) -> tuple[Any,str]:
    """明确岗位和环境后通过实际F0原件导入准备可查验票据。"""
    import hashlib

    provider = ComplianceProviderConfig(**scope,provider="tax",kind="tax",configuration_version=1,
        adapter_version="v1",base_url="http://127.0.0.1:9999",token_file=tmp_path/"token",
        callback_secret_file=tmp_path/"secret",callback_key_id="test",simulation=True,
        supports_idempotency=True,supports_query=True,supports_state_revision=True)
    policy = CompliancePolicy(**scope,version=1,enabled=True,tax_enabled=True,tax_provider="tax",
        tax_configuration_version=1,tax_executor_user_ids=[people["payer"].user_id],tax_steps=[step],
        open_periods=["2026-10"],supported_invoice_profiles=["urn:xuanpu:finance:synthetic:v1"])
    await store.configure_compliance([policy],[provider.public_descriptor()],simulation=True)
    who = FinancePrincipal("acme",people["owner"].user_id)
    await store.accept_import(who,scope["legal_entity_id"],[FileRef(file_id="cookie-file",revision=1,
        sha256=hashlib.sha256(RAW).hexdigest())],"source")
    await FinanceWorker(store,EncryptedLocalObjects(tmp_path/"originals",b"k"*32),OriginalGateway(),synthetic=True).run_once()
    invoice = (await store.list_invoices(who,scope["legal_entity_id"]))["items"][0]
    return provider,invoice["id"]


async def test_real_cookie_csrf_and_offline_model_tax_hitl(identity_world: Any,finance_database: Any,
                                                         tmp_path: Path,monkeypatch: Any) -> None:
    """真实平台身份签名、财务验签和PG完成两人批准与原经办执行。"""
    from xuanpu_finance.gateway import PlatformGateway

    async with identity_world() as ident:
        platform,store = ident.world.platform,finance_database
        people,cookies,scope,step = await f2_people_and_expenses(ident,store)
        provider,invoice_id = await prepare_finance(store,people,scope,step,tmp_path)
        key,seen = Ed25519PrivateKey.generate(),[]
        config = await f2_signer_config(ident,key)
        monkeypatch.setattr(platform.runtimes,"get",f2_no_model)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(platform)),base_url="https://xuanpu.example.com") as online, httpx.AsyncClient(transport=httpx.MockTransport(tax_response)) as external:
            gateway = PlatformGateway(online,tenant_tokens={"acme":await ident.system_key(role="tenant_member")})
            platform.manual_connectors = ManualConnectorService(packages=platform.packages,vault=platform.vault,
                audit=platform.audit,signer=HumanContextSigner(config=config,vault=platform.vault),
                transport=VerifiedFinanceTransport(finance_app(store,gateway,key),key,seen))
            calls = F2CookieCalls(ident,cookies)
            registry = ComplianceRegistry([provider],external,tokens={("tax",1):"test"},callback_secrets={("tax",1):"s"*32})
            worker = SettlementWorker(ComplianceQueueAdapter(store),registry,gateway)
            await cookie_tax_flow(calls,worker,invoice_id)
        assert seen and all(item["origin"]=="human" for item in seen)
        assert {item["sub"] for item in seen} == {people[name].user_id for name in ("owner","payer","approver")}


async def cookie_tax_flow(calls: Any,worker: Any,id: str) -> None:
    """受理、预检、实际专用审批和原经办确认都是独立真实请求。"""
    await calls.call("owner","verifyInvoice",{"id":id,"Idempotency-Key":"verify","body":{"expected_version":1}},write=True)
    assert await worker.run_once()
    state = await calls.call("payer","getInvoiceCompliance",{"id":id})
    assert state["verification_status"]=="passed"
    plan = await calls.action("payer",{"kind":"invoice","id":id,"version":state["version"]},"confirm_usage",accounting_period="2026-10")
    assert not plan["executable"]
    queue = await calls.call("approver","listReviewQueue",{"legal_entity_id":"company-a"})
    review = await calls.action("approver",{"kind":"action_plan","id":plan["id"],"version":1},"approve",workflow_step_id=queue["items"][0]["workflow_step_id"])
    await calls.execute("approver",review)
    ready = await calls.call("payer","getActionPlan",{"id":plan["id"]})
    assert ready["executable"]
    await calls.execute("payer",ready)
    assert await worker.run_once()
    confirmed = await calls.call("payer","getInvoiceCompliance",{"id":id})
    assert confirmed["snapshot"]["usage_status"]=="confirmed"

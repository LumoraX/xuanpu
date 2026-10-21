"""真实平台 cookie 经通用签名网关进入独立财务 PG，并在重建后继续人工审批。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import asyncpg
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.api import create_app as create_finance_app
from xuanpu_finance.auth import FinanceAuthenticator
from xuanpu_finance.expenses.models import ExpensePolicy
from xuanpu_finance.gateway import PlatformGateway
from xuanpu_finance.storage import FinanceStore
from xuanpu_server.api import create_app
from xuanpu_server.config import CallContextConfig
from xuanpu_server.manual_connectors import ManualConnectorService
from xuanpu_server.runtime.call_context import HumanContextSigner
from xuanpu_spec import pack_package, request_fingerprint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def finance_database() -> AsyncIterator[FinanceStore]:
    """仅在显式 finance_test 独立库内使用随机 schema，结束后完整清理。"""
    dsn = os.environ.get('FINANCE_TEST_DSN')
    if not dsn:
        pytest.skip('须显式提供独立 FINANCE_TEST_DSN')
    admin = await asyncpg.connect(dsn)
    database = await admin.fetchval('SELECT current_database()')
    if not isinstance(database, str) or not database.startswith('finance_test'):
        await admin.close()
        pytest.fail('集成测试只允许 finance_test 前缀的独立库')
    schema = 'finance_gateway_' + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8, server_settings={'search_path': schema})
    assert pool is not None
    service = FinanceStore(pool)
    await service.migrate()
    try:
        yield service
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


class VerifiedFinanceTransport(httpx.AsyncBaseTransport):
    """观察最终请求后交给真实财务 ASGI 认证与 PG，绝不替换认证结果。"""

    def __init__(self, app: Any, key: Ed25519PrivateKey, seen: list[Any]) -> None:
        """保存独立验签公钥和真实服务传输。"""
        self.inner, self.key, self.seen = httpx.ASGITransport(app), key.public_key(), seen

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """同时检查签名来源和最终请求绑定，再执行真实FinanceAuthenticator。"""
        claims = jwt.decode(request.headers['X-Xuanpu-Context'], self.key, algorithms=['EdDSA'],
                            audience='xuanpu-finance', issuer='platform')
        assert claims['origin'] == 'human' and claims['session_id'].startswith('interactive:')
        assert claims['exp'] - claims['iat'] == 60
        assert claims['request_sha256'] == request_fingerprint(request.method, request.url.path,
            list(request.url.params.multi_items()), json.loads(request.content) if request.content else None,
            request.headers.get('Idempotency-Key', ''), request.headers.get('Content-Type', ''))
        self.seen.append(claims)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        """每个出口客户端关闭时清理内部ASGI资源。"""
        await self.inner.aclose()


def finance_app(store: FinanceStore, gateway: PlatformGateway, key: Ed25519PrivateKey) -> Any:
    """创建真实认证器与真实独立存储，后台导入worker无需启动。"""
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    auth = FinanceAuthenticator(service_token='finance-service-private', issuer='platform',
        audience='xuanpu-finance', public_keys={'key1': public}, store=store, gateway=gateway)
    return create_finance_app(store=store, auth=auth, start_worker=False)


async def invoke(http: httpx.AsyncClient, headers: dict[str, str], operation: str,
                 arguments: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
    """通过平台公开cookie人工入口调用，断言保留业务服务结构化状态。"""
    response = await http.post('/v1/integration-actions/finance/finance/' + operation,
        headers={**headers, 'X-Requested-By': 'xuanpu', 'Origin': str(http.base_url).rstrip('/')}, json={'arguments': arguments, 'confirmed': confirmed})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['is_error'] is False, result
    assert result['truncated'] is False
    body: dict[str, Any] = result['body']
    return body


async def preview_and_execute(http: httpx.AsyncClient, headers: dict[str, str],
                              claim: dict[str, Any], action: str, **parameters: Any) -> dict[str, Any]:
    """显式分开预检与人工确认，不把工具调用成功当成业务批准。"""
    plan = await invoke(http, headers, 'previewFinanceAction', {'Idempotency-Key': str(uuid4()),
        'body': {'action': action, 'target_type': 'expense_claim', 'target_id': claim['id'],
                 'expected_version': claim['version'], 'parameters': parameters}}, confirmed=True)
    assert plan['executable'], plan['blockers']
    assert plan['target_version'] == claim['version']
    assert plan['effects']
    return await invoke(http, headers, 'executeFinanceAction', {'Idempotency-Key': str(uuid4()),
        'body': {'plan_id': plan['id'], 'plan_digest': plan['digest']}}, confirmed=True)


async def test_cookie_gateway_real_finance_pg_rebuild_approval(
    identity_world: Any, finance_database: FinanceStore, tmp_path: Path, monkeypatch: Any,
) -> None:
    """员工提交、独立审批人在重建后批准；每次都真实验签和在线复核身份。"""
    async with identity_world() as ident:
        platform, store = ident.world.platform, finance_database
        owner, approver, reviewer = await ident.user(name='报销人'), await ident.user(name='审批人'), await ident.user(name='复核人')
        owner_cookie, approver_cookie = ident.cookie(await ident.login(owner)), ident.cookie(await ident.login(approver))
        reviewer_cookie = ident.cookie(await ident.login(reviewer))
        await store.seed_company('acme', 'company-a', '开发公司', 'BUYER')
        for user, role in [(owner, 'collector'), (approver, 'approver'), (reviewer, 'reviewer')]:
            await store.grant('acme', user.user_id, 'company-a', role)
        await store.configure_expenses([ExpensePolicy(tenant_id='acme', legal_entity_id='company-a',
            version=1, enabled=True, record_mode='internal_record', categories=[{'id':'travel','name':'交通'}],
            cost_centers=[], steps=[{'id':'manager','name':'负责人审批','approver_user_id':approver.user_id}, {'id':'finance','name':'财务复核','approver_user_id':reviewer.user_id}],
            exception_owner_user_id=reviewer.user_id, require_verified_invoice=False)])
        key, seen = Ed25519PrivateKey.generate(), []
        config = CallContextConfig(issuer='platform', keyId='key1', privateKeySecret='context-key',
            allowedAudiences={'http://127.0.0.1:8443': ['xuanpu-finance']})
        await platform.vault.put('acme', 'context-key', key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), actor='test')
        await platform.vault.put('acme', 'finance-service-token', 'finance-service-private', actor='test')
        await platform.packages.publish(ident.world.admin, pack_package(Path(__file__).resolve().parents[4] / 'examples/finance'), activate=True)
        async def no_model(*args: Any, **kwargs: Any) -> Any:
            """真实人工入口不可调用故障模型。"""
            pytest.fail('人工流程启动了模型运行时')
        monkeypatch.setattr(platform.runtimes, 'get', no_model)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(platform)), base_url='https://xuanpu.example.com') as online:
            gateway = PlatformGateway(online, tenant_tokens={'acme': await ident.system_key(role='tenant_member')})
            transport = VerifiedFinanceTransport(finance_app(store, gateway, key), key, seen)
            platform.manual_connectors = ManualConnectorService(packages=platform.packages, vault=platform.vault,
                audit=platform.audit, signer=HumanContextSigner(config=config, vault=platform.vault), transport=transport)
            claim = await invoke(ident.http, owner_cookie, 'createExpenseClaim', {'Idempotency-Key': 'draft', 'body': {
                'legal_entity_id':'company-a','reason':'客户拜访交通','lines':[{'category_id':'travel','amount':'25.00','occurred_on':'2026-10-21','reason':'地铁'}]}}, confirmed=True)
            assert claim['status'] == 'draft' and claim['total'] == '25.00'
            await preview_and_execute(ident.http, owner_cookie, claim, 'submit_claim')
            submitted = await invoke(ident.http, owner_cookie, 'getExpenseClaim', {'id': claim['id']})
            assert submitted['status'] == 'under_review' and submitted['record_mode'] == 'internal_record'
            first_queue = await invoke(ident.http, approver_cookie, 'listReviewQueue', {'legal_entity_id':'company-a'})
            await preview_and_execute(ident.http, approver_cookie, submitted, 'approve', workflow_step_id=first_queue['items'][0]['workflow_step_id'])
            intermediate = await invoke(ident.http, owner_cookie, 'getExpenseClaim', {'id':claim['id']})
            assert intermediate['status'] == 'under_review'
            assert intermediate['version'] == submitted['version']
            assert intermediate['workflow_version'] > submitted['workflow_version']
            schema = await store.pool.fetchval('SHOW search_path')
            rebuilt_pool = await asyncpg.create_pool(os.environ['FINANCE_TEST_DSN'], min_size=1, max_size=4,
                                                     server_settings={'search_path': schema})
            assert rebuilt_pool is not None
            rebuilt_store = FinanceStore(rebuilt_pool)
            sibling = ident.world.sibling('manual-rebuild', data_dir=tmp_path/'rebuilt', call_context=config,
                manual_http_transport=VerifiedFinanceTransport(finance_app(rebuilt_store, gateway, key), key, seen))
            monkeypatch.setattr(sibling.runtimes, 'get', no_model)
            try:
                await sibling.start()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(sibling)), base_url=str(ident.http.base_url)) as rebuilt_http:
                    queue = await invoke(rebuilt_http, reviewer_cookie, 'listReviewQueue', {'legal_entity_id':'company-a'})
                    item = queue['items'][0]
                    assert item['target_id'] == claim['id']
                    restored = await invoke(rebuilt_http, reviewer_cookie, 'getExpenseClaim', {'id':claim['id']})
                    assert restored['workflow']['current_step_id'] == item['workflow_step_id']
                    await preview_and_execute(rebuilt_http, reviewer_cookie, restored, 'approve', workflow_step_id=item['workflow_step_id'])
                    final = await invoke(rebuilt_http, owner_cookie, 'getExpenseClaim', {'id':claim['id']})
                    assert final['status'] == 'approved' and final['total'] == '25.00'
            finally:
                await sibling.close()
                await rebuilt_pool.close()
        assert {item['sub'] for item in seen} == {owner.user_id, approver.user_id, reviewer.user_id}
        assert all(item['origin'] == 'human' for item in seen)
        assert await store.pool.fetchval('SELECT count(*) FROM finance_context_replay') == len(seen)


def f2_ref(kind: str, id: str) -> dict[str, str]:
    """使用明确的受控模拟主数据来源。"""
    return {"kind": kind, "source": "controlled", "id": id, "version": "v1", "sha256": "a" * 64}


async def f2_people_and_expenses(ident: Any, store: FinanceStore) -> tuple[Any, Any, Any, Any]:
    """建立实际登录员工、分离岗位与模拟单据的业务审批制度。"""
    people = {
        name: await ident.user(name=name) for name in ("owner", "payer", "accountant", "approver")
    }
    cookies = {name: ident.cookie(await ident.login(user)) for name, user in people.items()}
    scope = {"tenant_id": "acme", "legal_entity_id": "company-a"}
    await store.seed_company("acme", "company-a", "模拟公司", "BUYER")
    for name, user in people.items():
        await store.grant(
            "acme",
            user.user_id,
            "company-a",
            "collector" if name == "owner" else "approver" if name == "approver" else "reviewer",
        )
    step = {"id": "review", "name": "专用审批", "approver_user_id": people["approver"].user_id}
    await store.configure_expenses(
        [
            ExpensePolicy(
                **scope,
                version=1,
                enabled=True,
                record_mode="invoice_backed",
                categories=[{"id": "other", "name": "其他"}],
                cost_centers=[],
                steps=[step],
                exception_owner_user_id=people["accountant"].user_id,
                require_verified_invoice=False,
            )
        ]
    )
    return people, cookies, scope, step


def f2_settlement_policy(scope: Any, people: Any, step: Any) -> Any:
    """付款和记账经办分离，专用审批仍由独立实际员工承担。"""
    from xuanpu_finance.settlement.models import SettlementPolicy

    return SettlementPolicy(
        **scope,
        version=1,
        enabled=True,
        payment_provider="isolated",
        payment_configuration_version=1,
        posting_provider="isolated",
        posting_configuration_version=1,
        ledger_id="test-ledger",
        open_periods=["2026-10"],
        payment_executor_user_ids=[people["payer"].user_id],
        posting_executor_user_ids=[people["accountant"].user_id],
        payment_steps=[step],
        posting_steps=[step],
        reversal_steps=[step],
        require_verified_invoice=False,
    )


async def f2_settlements(
    store: FinanceStore, people: Any, scope: Any, step: Any, tmp_path: Path
) -> Any:
    """仅为隔离模拟配置受控供应商、采购依据与脱敏收款账户。"""
    from xuanpu_finance.settlement.models import BusinessSnapshot, PayeeAccount
    from xuanpu_finance.settlement.provider_models import ProviderConfig

    provider = ProviderConfig(
        **scope,
        provider="isolated",
        configuration_version=1,
        adapter_version="http-v1",
        base_url="http://127.0.0.1:9999",
        token_file=tmp_path / "token",
        callback_secret_file=tmp_path / "callback",
        callback_key_id="test",
        simulation=True,
        supports_idempotency=True,
        supports_query=True,
    )
    documents = [
        BusinessSnapshot(
            **scope,
            ref=f2_ref(kind, kind),
            name=kind,
            verification_state="verified",
            allowed_user_ids=[people["owner"].user_id],
        )
        for kind in ("supplier", "contract")
    ]
    account = PayeeAccount(
        **scope,
        ref=f2_ref("supplier_account", "account"),
        subject_id="supplier",
        account_name="模拟供应商",
        masked_account="****0001",
        provider_account_id="test-account",
        verified=True,
        allowed_user_ids=[people["payer"].user_id],
    )
    await store.configure_settlements(
        [f2_settlement_policy(scope, people, step)],
        documents,
        [account],
        [provider.public_descriptor()],
        simulation=True,
    )
    return provider


async def f2_invoice(store: FinanceStore, people: Any) -> None:
    """只插入明确未查验的模拟票据，不能冒充正式供应商查验事实。"""
    invoice = {
        "id": "test-invoice",
        "version": 1,
        "legal_entity_id": "company-a",
        "gross": "1000.00",
        "tax": "0.00",
        "buyer_tax_id": "BUYER",
        "is_red": False,
        "ticket_status": "normal",
        "verification_status": "not_checked",
        "authority_status": "unverified",
        "available_amount": "1000.00",
    }
    await store.pool.execute(
        "INSERT INTO finance_invoice(tenant_id,legal_entity_id,id,owner_id,seller_tax_id,number,issue_date,data,canonical_digest) VALUES('acme','company-a','test-invoice',$1,'SELLER','TEST','2026-10-21',$2::jsonb,'test')",
        people["owner"].user_id,
        json.dumps(invoice),
    )


async def f2_signer_config(ident: Any, key: Ed25519PrivateKey) -> CallContextConfig:
    """发布实际业务包并在真实平台凭据库配置员工人工网关签名。"""
    platform = ident.world.platform
    config = CallContextConfig(
        issuer="platform",
        keyId="key1",
        privateKeySecret="context-key",
        allowedAudiences={"http://127.0.0.1:8443": ["xuanpu-finance"]},
    )
    await platform.vault.put(
        "acme",
        "context-key",
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        actor="test",
    )
    await platform.vault.put(
        "acme", "finance-service-token", "finance-service-private", actor="test"
    )
    await platform.packages.publish(
        ident.world.admin,
        pack_package(Path(__file__).resolve().parents[4] / "examples/finance"),
        activate=True,
    )
    return config


async def f2_no_model(*args: Any, **kwargs: Any) -> Any:
    """人工通路即使模型完全故障也必须正常工作。"""
    pytest.fail("F2人工通路调用了模型")


def f2_provider_response(request: httpx.Request, sent: list[dict[str, Any]]) -> httpx.Response:
    """仅隔离外部网络，真实HTTP序列化与响应校验保持启用。"""
    body = json.loads(request.content) if request.method == "POST" else sent[0]
    if request.method == "POST":
        sent.append(body)
    fields = (
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
    )
    result = {name: body[name] for name in fields}
    result.update(
        status="accepted" if request.method == "POST" else "succeeded",
        external_order_id="order",
        transaction_id="transaction",
        evidence=[
            {
                "id": "proof",
                "sha256": "b" * 64,
                "media_type": "application/pdf",
                "object_ref": "provider:proof",
            }
        ],
    )
    return httpx.Response(200, json=result)


class F2CookieCalls:
    """保留实际员工cookie与通用人工调用，辅助函数不绕过签名认证。"""

    def __init__(self, ident: Any, cookies: Any) -> None:
        """保存当前测试实际登录会话。"""
        self.ident, self.cookies = ident, cookies

    async def call(
        self, user: str, operation: str, arguments: dict[str, Any], *, write: bool = False
    ) -> dict[str, Any]:
        """当前员工cookie始终由真实平台决定human来源。"""
        return await invoke(
            self.ident.http, self.cookies[user], operation, arguments, confirmed=write
        )

    async def action(
        self, user: str, target: dict[str, Any], name: str, **parameters: Any
    ) -> dict[str, Any]:
        """通用预检明确使用实际目标类型。"""
        return await self.call(
            user,
            "previewFinanceAction",
            {
                "Idempotency-Key": uuid4().hex,
                "body": {
                    "action": name,
                    "target_type": target.get("kind", "payable"),
                    "target_id": target["id"],
                    "expected_version": target["version"],
                    "parameters": parameters,
                },
            },
            write=True,
        )

    async def execute(self, user: str, plan: dict[str, Any]) -> dict[str, Any]:
        """真实人工确认与专用批准分为独立事务。"""
        return await self.call(
            user,
            "executeFinanceAction",
            {
                "Idempotency-Key": uuid4().hex,
                "body": {"plan_id": plan["id"], "plan_digest": plan["digest"]},
            },
            write=True,
        )


async def f2_create_allocated_document(calls: F2CookieCalls) -> dict[str, Any]:
    """经cookie人工网关新建应付并关联原有模拟票据。"""
    doc = await calls.call(
        "owner",
        "createPayable",
        {
            "Idempotency-Key": "draft",
            "body": {
                "legal_entity_id": "company-a",
                "supplier_ref": f2_ref("supplier", "supplier"),
                "amount": "1000.00",
                "due_date": "2026-11-01",
                "business_refs": [f2_ref("contract", "contract")],
                "reason": "隔离采购",
            },
        },
        write=True,
    )
    await calls.call(
        "owner",
        "createInvoiceAllocation",
        {
            "Idempotency-Key": "allocation",
            "body": {
                "invoice_id": "test-invoice",
                "invoice_version": 1,
                "document_id": doc["id"],
                "document_kind": "payable",
                "expected_document_version": doc["version"],
                "amount": "1000.00",
                "tax": "0.00",
            },
        },
        write=True,
    )
    return await calls.call("owner", "getPayable", {"id": doc["id"]})


async def f2_approved_payment(calls: F2CookieCalls) -> tuple[dict[str, Any], dict[str, Any]]:
    """先取原业务批准，再由实际分派人批准固定付款，原经办人明确执行。"""
    doc = await f2_create_allocated_document(calls)
    await calls.execute("owner", await calls.action("owner", doc, "submit_payable"))
    doc = await calls.call("owner", "getPayable", {"id": doc["id"]})
    queue = await calls.call("approver", "listReviewQueue", {"legal_entity_id": "company-a"})
    await calls.execute(
        "approver",
        await calls.action(
            "approver", doc, "approve", workflow_step_id=queue["items"][0]["workflow_step_id"]
        ),
    )
    doc = await calls.call("payer", "getPayable", {"id": doc["id"]})
    plan = await calls.action(
        "payer",
        doc,
        "execute_payment",
        amount="600.00",
        payee_ref=f2_ref("supplier_account", "account"),
    )
    assert not plan["executable"]
    queue = await calls.call("approver", "listReviewQueue", {"legal_entity_id": "company-a"})
    item = next(row for row in queue["items"] if row["target_id"] == plan["id"])
    await calls.execute(
        "approver",
        await calls.action(
            "approver",
            {**plan, "kind": "action_plan", "version": 1},
            "approve",
            workflow_step_id=item["workflow_step_id"],
        ),
    )
    ready = await calls.call("payer", "getActionPlan", {"id": plan["id"]})
    assert ready["executable"] and ready["digest"] == plan["digest"]
    return doc, await calls.execute("payer", ready)


async def f2_reconcile_payment(
    store: FinanceStore,
    calls: F2CookieCalls,
    people: Any,
    doc: dict[str, Any],
    operation: dict[str, Any],
    worker: Any,
    sent: list[dict[str, Any]],
) -> None:
    """受理仍未付款，按原请求查单后才确认模拟渠道完成，并断言没有重复发送。"""
    from xuanpu_finance.models import FinancePrincipal

    await worker.run_once()
    assert (await calls.call("payer", "getPayable", {"id": doc["id"]}))["paid_amount"] == "0.00"
    principal = FinancePrincipal("acme", people["payer"].user_id, origin="human")
    current = await store.get_operation(principal, operation["id"])
    await calls.call(
        "payer",
        "reconcileOperation",
        {
            "id": operation["id"],
            "Idempotency-Key": "query",
            "body": {"expected_version": current["version"]},
        },
        write=True,
    )
    await worker.run_once(allow_dispatch=False)
    assert (await calls.call("payer", "getPayable", {"id": doc["id"]}))["paid_amount"] == "600.00"
    assert len(sent) == 1


async def test_f2_cookie_gateway_special_approval_and_payment(
    identity_world: Any,
    finance_database: FinanceStore,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """实际平台身份与签名贯通专用批准和付款；外部HTTP传输显式隔离模拟。"""
    from xuanpu_finance.settlement.providers import ProviderRegistry
    from xuanpu_finance.settlement.worker import SettlementWorker

    async with identity_world() as ident:
        platform, store = ident.world.platform, finance_database
        people, cookies, scope, step = await f2_people_and_expenses(ident, store)
        provider = await f2_settlements(store, people, scope, step, tmp_path)
        await f2_invoice(store, people)
        key, seen, sent = Ed25519PrivateKey.generate(), [], []
        config = await f2_signer_config(ident, key)
        monkeypatch.setattr(platform.runtimes, "get", f2_no_model)
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(create_app(platform)),
                base_url="https://xuanpu.example.com",
            ) as online,
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: f2_provider_response(request, sent))
            ) as external,
        ):
            gateway = PlatformGateway(
                online, tenant_tokens={"acme": await ident.system_key(role="tenant_member")}
            )
            platform.manual_connectors = ManualConnectorService(
                packages=platform.packages,
                vault=platform.vault,
                audit=platform.audit,
                signer=HumanContextSigner(config=config, vault=platform.vault),
                transport=VerifiedFinanceTransport(finance_app(store, gateway, key), key, seen),
            )
            calls = F2CookieCalls(ident, cookies)
            doc, operation = await f2_approved_payment(calls)
            registry = ProviderRegistry(
                [provider],
                external,
                tokens={("isolated", 1): "test"},
                callback_secrets={("isolated", 1): "s" * 32},
            )
            await f2_reconcile_payment(
                store,
                calls,
                people,
                doc,
                operation,
                SettlementWorker(store, registry, gateway),
                sent,
            )
        assert all(item["origin"] == "human" for item in seen)
        assert {item["sub"] for item in seen} == {
            people[name].user_id for name in ("owner", "payer", "approver")
        }

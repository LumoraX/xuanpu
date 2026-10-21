"""工作台 HTTP 权限、严格请求和安全下载。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
from xuanpu_server.api import create_app
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.identity import LoginSession, UserRecord
from xuanpu_server.identity.config import IdentityConfig
from xuanpu_server.identity.cookies import login_cookie_name, new_login_token
from xuanpu_server.identity.service import IdentitySettings


@pytest.fixture
async def api(world_opener, local_sandboxes):
    """真实浏览器认证与两名员工身份。"""
    settings = IdentitySettings.from_config("https://xuanpu.example.com", IdentityConfig())
    world = await world_opener(
        store=InMemoryControlPlane(), sandboxes=local_sandboxes(), identity_settings=settings
    )
    await world.platform.reminders.close()
    users = {}
    for user_id in ("usr_a", "usr_b"):
        await world.platform.identity.store.create_user(
            UserRecord(
                tenant_id="acme", user_id=user_id, display_name=user_id, created_at=time.time()
            )
        )
        token, hashed = new_login_token()
        now = time.time()
        await world.platform.identity.store.create_login_session(
            LoginSession(
                token_hash=hashed,
                tenant_id="acme",
                user_id=user_id,
                created_at=now,
                expires_at=now + 3600,
                last_seen_at=now,
            )
        )
        users[user_id] = {"Cookie": f"{login_cookie_name(settings.cookie_secure)}={token}"}
    app = create_app(world.platform)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://xuanpu.example.com"
    ) as client:
        try:
            yield SimpleNamespace(world=world, client=client, users=users, app=app)
        finally:
            await world.platform.close()


def headers(api, user="usr_a"):
    """实际同源 CSRF 标识。"""
    return {**api.users[user], "X-Requested-By": "xuanpu", "Origin": "https://xuanpu.example.com"}


async def test_http_crud_owner_and_machine_isolation(api):
    """真实路由拒绝机器身份、CSRF和员工越权。"""
    client = api.client
    body = {"kind": "todo", "data": {"title": "任务"}, "request_id": "one"}
    assert (
        await client.post("/v1/workspace/items", json=body, headers=api.users["usr_a"])
    ).status_code == 403
    created = await client.post("/v1/workspace/items", json=body, headers=headers(api))
    assert created.status_code == 200, created.text
    item = created.json()
    assert (await client.get("/v1/workspace/items", headers=api.users["usr_b"])).json() == {
        "items": []
    }
    assert (
        await client.get(f"/v1/workspace/items/{item['id']}", headers=api.users["usr_b"])
    ).status_code == 404
    assert (
        await client.get(
            "/v1/workspace/settings", headers={"Authorization": f"Bearer {api.world.admin_key}"}
        )
    ).status_code == 403
    assert (
        await client.post(
            "/v1/workspace/items", json={**body, "user_id": "usr_b"}, headers=headers(api)
        )
    ).status_code == 400
    assert (
        await client.delete(f"/v1/workspace/items/{item['id']}?version=1", headers=headers(api))
    ).json()["trashed_at"] is not None
    assert (
        await client.post(
            f"/v1/workspace/items/{item['id']}/restore?version=2", headers=headers(api)
        )
    ).json()["version"] == 3


async def test_http_upload_versions_and_safe_download(api):
    """上传正本、版本读回和浏览器强制附件响应。"""
    created = await api.client.post(
        "/v1/workspace/files",
        params={"name": "报告.html", "request_id": "upload"},
        content=b"<script>alert(1)</script>",
        headers={**headers(api), "Content-Type": "text/html"},
    )
    assert created.status_code == 200, created.text
    item = created.json()
    downloaded = await api.client.get(
        f"/v1/workspace/files/{item['id']}/content", headers=api.users["usr_a"]
    )
    assert downloaded.status_code == 200 and downloaded.content == b"<script>alert(1)</script>"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert (
        await api.client.get(
            f"/v1/workspace/files/{item['id']}/content", headers=api.users["usr_b"]
        )
    ).status_code == 404
    assert (
        await api.client.get(
            f"/v1/workspace/files/{item['id']}/versions", headers=api.users["usr_a"]
        )
    ).json()["items"][0]["revision"] == 1
    assert (
        await api.client.post(
            "/v1/workspace/messages",
            json={"role": "assistant", "text": "injection"},
            headers=headers(api),
        )
    ).status_code in (404, 405)
    invalid = await api.client.post(
        "/v1/workspace/items",
        json={
            "kind": "event",
            "data": {"title": "x", "start_at": 5, "end_at": 4},
            "request_id": "bad",
        },
        headers=headers(api),
    )
    assert invalid.status_code == 400


async def test_history_before_cursor_http_bounds(api):
    """上界游标拒绝零值和数据库整数溢出。"""
    for value in ("0", "9223372036854775808", "nonsense"):
        response = await api.client.get(
            "/v1/workspace/messages", params={"before_id": value}, headers=api.users["usr_a"]
        )
        assert response.status_code == 400


async def test_integration_bridge_fixed_revision_and_identity(api):
    """桥仅开放已认证员工的固定版本，摘要或归属不符拒绝。"""
    import hashlib

    response = await api.client.get('/v1/integration-gateway/identity', headers=api.users['usr_a'])
    assert response.status_code == 200
    assert response.json() == {'tenant_id': 'acme', 'user_id': 'usr_a', 'active': True}
    machine = {'Authorization': f'Bearer {api.world.admin_key}'}
    assert (await api.client.get('/v1/integration-gateway/identity', headers=machine)).status_code == 403
    created = await api.client.post('/v1/workspace/files', params={'name': 'a.txt', 'request_id': 'bridge'},
                                    content=b'first', headers={**headers(api), 'Content-Type': 'text/plain'})
    item = created.json()
    await api.client.put(f"/v1/workspace/files/{item['id']}/content", params={'version': item['version'],
                         'request_id': 'bridge-update'}, content=b'second',
                         headers={**headers(api), 'Content-Type': 'text/plain'})
    path = f"/v1/integration-gateway/files/{item['id']}"
    params = {'revision': 1, 'sha256': hashlib.sha256(b'first').hexdigest()}
    result = await api.client.get(path, params=params, headers=api.users['usr_a'])
    assert result.status_code == 200 and result.content == b'first'
    assert (await api.client.get(path, params=params, headers=api.users['usr_b'])).status_code == 404
    assert (await api.client.get(path, headers=api.users['usr_a'])).status_code == 400
    params['sha256'] = '0' * 64
    assert (await api.client.get(path, params=params, headers=api.users['usr_a'])).status_code == 400


async def test_bridge_trusted_act_as_and_disabled_employee(api):
    """独立服务需显式代员工授权；员工停用即时拒绝读取。"""
    from xuanpu_server.platform import ROOT_PRINCIPAL

    key = await api.world.platform.issue_key(ROOT_PRINCIPAL, tenant_id='acme', name='finance',
                                             role='tenant_member', act_as_users=True)
    machine = {'Authorization': f'Bearer {key.key}'}
    employee = {**machine, 'X-Xuanpu-Act-As': 'usr_a'}
    path = '/v1/integration-gateway/identity'
    assert (await api.client.get(path, headers=machine)).status_code == 403
    assert (await api.client.get(path, headers=employee)).json()['user_id'] == 'usr_a'
    forged = {**machine, 'X-Xuanpu-Act-As': 'foreign-user'}
    assert (await api.client.get(path, headers=forged)).status_code == 403
    unauthorized = {'Authorization': f'Bearer {api.world.admin_key}', 'X-Xuanpu-Act-As': 'usr_a'}
    assert (await api.client.get(path, headers=unauthorized)).status_code == 403
    await api.world.platform.identity.disable_user('acme', 'usr_a', 'admin', ROOT_PRINCIPAL, keep_an_admin=False)
    assert (await api.client.get(path, headers=employee)).status_code == 403
    assert (await api.client.get(path, headers=api.users['usr_a'])).status_code == 401


async def test_bridge_files_reject_root_machine_foreign_tenant_and_disabled(api):
    """直接文件路由对四类未授权主体拒绝，管理员也不默认拥有员工文件。"""
    import hashlib

    from xuanpu_server.platform import ROOT_PRINCIPAL

    created = await api.client.post('/v1/workspace/files', params={'name': 'receipt.pdf', 'request_id': 'guards'},
        content=b'private-file', headers={**headers(api), 'Content-Type': 'application/pdf'})
    item = created.json()
    path = f"/v1/integration-gateway/files/{item['id']}"
    params = {'revision': 1, 'sha256': hashlib.sha256(b'private-file').hexdigest()}
    for token in (api.world.root_key, api.world.admin_key, api.world.member_key):
        assert (await api.client.get(path, params=params, headers={'Authorization': f'Bearer {token}'})).status_code == 403
    await api.world.platform.create_tenant(ROOT_PRINCIPAL, tenant_id='foreign', name='Foreign')
    await api.world.platform.identity.store.create_user(UserRecord(tenant_id='foreign', user_id='usr_foreign',
        display_name='foreign', created_at=time.time()))
    foreign_key = await api.world.platform.issue_key(ROOT_PRINCIPAL, tenant_id='foreign', name='foreign-service',
        role='tenant_member', act_as_users=True)
    foreign = {'Authorization': f'Bearer {foreign_key.key}', 'X-Xuanpu-Act-As': 'usr_foreign'}
    assert (await api.client.get(path, params=params, headers=foreign)).status_code == 404
    key = await api.world.platform.issue_key(ROOT_PRINCIPAL, tenant_id='acme', name='files-service',
        role='tenant_member', act_as_users=True)
    owner = {'Authorization': f'Bearer {key.key}', 'X-Xuanpu-Act-As': 'usr_a'}
    assert (await api.client.get(path, params=params, headers=owner)).status_code == 200
    await api.world.platform.identity.disable_user('acme', 'usr_a', 'admin', ROOT_PRINCIPAL, keep_an_admin=False)
    assert (await api.client.get(path, params=params, headers=owner)).status_code == 403
    assert (await api.client.get(path, params=params, headers=api.users['usr_a'])).status_code == 401


@pytest.fixture
async def finance_bridge_store():
    """只在显式独立测试库的随机 schema 中运行跨服务联调。"""
    import os
    from uuid import uuid4

    import asyncpg
    from xuanpu_finance.storage import FinanceStore

    dsn = os.environ.get('FINANCE_TEST_DSN')
    if not dsn:
        pytest.skip('跨服务联调须提供独立 FINANCE_TEST_DSN')
    schema = 'finance_bridge_test_' + uuid4().hex
    admin = await asyncpg.connect(dsn)
    actual_database = await admin.fetchval("SELECT current_database()")
    if not actual_database.startswith("finance_test"):
        await admin.close()
        pytest.fail("只允许 finance_test 开头的独立测试数据库")
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, server_settings={'search_path': schema})
    store = FinanceStore(pool)
    await store.migrate()
    try:
        yield store
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def finance_bridge_client(api, store, objects):
    """连接真实平台身份文件桥和财务ASGI，JWT私钥只在租户保管库。"""
    import taifeng
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from xuanpu_finance.api import create_app as finance_app
    from xuanpu_finance.auth import FinanceAuthenticator
    from xuanpu_finance.gateway import PlatformGateway
    from xuanpu_finance.invoices.worker import FinanceWorker
    from xuanpu_server.config import CallContextConfig
    from xuanpu_server.platform import ROOT_PRINCIPAL
    from xuanpu_server.runtime.call_context import CallContextSigner
    from xuanpu_server.runtime.connectors import ConnectorGate
    from xuanpu_server.runtime.events import SubmissionIndex
    from xuanpu_server.runtime.gate_audit import ConnectorToolInfo
    from xuanpu_server.runtime.http_api import HttpApiClient
    from xuanpu_spec.manifest import HttpAuth
    from xuanpu_spec.openapi import Operation, Parameter

    key = Ed25519PrivateKey.generate()
    platform = api.world.platform
    await platform.vault.put('acme', 'context-key', key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), actor='test')
    await platform.vault.put('acme', 'finance-service', 'test-service', actor='test')
    service_key = await platform.issue_key(ROOT_PRINCIPAL, tenant_id='acme', name='finance-bridge',
                                          role='tenant_member', act_as_users=True)
    gateway = PlatformGateway(api.client, tenant_tokens={'acme': service_key.key})
    auth = FinanceAuthenticator(service_token='test-service', issuer='platform', audience='finance',
        public_keys={'bridge': key.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo).decode()}, store=store, gateway=gateway)
    worker = FinanceWorker(store, objects, gateway, synthetic=True)
    app = finance_app(store=store, auth=auth, worker=worker, start_worker=False)
    signer = CallContextSigner(config=CallContextConfig(issuer='platform', keyId='bridge',
        privateKeySecret='context-key', allowedAudiences={'https://finance.example': ['finance']}),
        store=platform.store, vault=platform.vault)
    client = HttpApiClient(base_url='https://finance.example', auth=HttpAuth(type='bearer', secret='finance-service'),
        tenant_id='acme', vault=platform.vault, timeout_seconds=5, transport=httpx.ASGITransport(app),
        context_signer=signer, context_audience='finance')
    operation = Operation('importInvoices', 'post', '/v1/finance/invoice-imports', '', parameters=(
        Parameter('Idempotency-Key', 'header', True, {}),), request_body={}, request_body_required=True)
    async def invoke(args):
        """保留真实HTTP客户端构造、签名和财务鉴权。"""
        return await client.call(operation, args)
    await api.world.publish()
    who = await platform.authenticate(service_key.key, act_as='usr_a')
    session = await platform.sessions.create(who, package='support-desk')
    submissions = SubmissionIndex()
    submissions.register('submission-bridge', session.session_id, 'forged')
    gate = ConnectorGate(invoke=invoke, info=ConnectorToolInfo('finance', 'http', 'importInvoices', 'write'),
        full_name='api__finance__importInvoices', tenant_id='acme', policy=taifeng.PermissionPolicy(default_mode='allow'),
        audit=platform.audit, submissions=submissions, timeout_seconds=10)
    context = taifeng.ToolContext(call_id='actual-import', cancel=taifeng.CancellationToken(), thread_id='trusted-thread',
                                 extras={'submission_id': 'submission-bridge'})
    return client, gate, context, worker


async def test_real_upload_signed_finance_and_worker_download(api, finance_bridge_store, tmp_path):
    """真实员工上传→平台签名→独立PG财务受理→worker经真实文件桥建立加密副本。"""
    import hashlib
    import json

    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.objects import EncryptedLocalObjects

    sample = b'''<Invoice xmlns="urn:xuanpu:finance:synthetic:v1"><Number>BRIDGE-001</Number><IssueDate>2026-10-21</IssueDate><SellerName>Seller</SellerName><SellerTaxId>SELLER</SellerTaxId><BuyerName>Buyer</BuyerName><BuyerTaxId>BUYER</BuyerTaxId><Currency>CNY</Currency><Amount>100.00</Amount><TaxAmount>6.00</TaxAmount><Total>106.00</Total><Lines><Line><Name>Service</Name><Quantity>1</Quantity><UnitPrice>100.00</UnitPrice><Amount>100.00</Amount><TaxRate>0.06</TaxRate><TaxAmount>6.00</TaxAmount></Line></Lines></Invoice>'''
    seen = []
    @api.app.middleware('http')
    async def record_bridge(request, call_next):
        """记录实际网关路由，仅观察，不替换鉴权或文件读取。"""
        if request.url.path.startswith('/v1/integration-gateway/'):
            seen.append((request.url.path, request.headers.get('X-Xuanpu-Act-As'), dict(request.query_params)))
        return await call_next(request)
    uploaded = await api.client.post('/v1/workspace/files', params={'name': 'invoice.xml', 'request_id': 'invoice-source'},
        content=sample, headers={**headers(api), 'Content-Type': 'application/xml'})
    assert uploaded.status_code == 200, uploaded.text
    file = {'file_id': uploaded.json()['id'], 'revision': 1, 'sha256': hashlib.sha256(sample).hexdigest()}
    store = finance_bridge_store
    await store.seed_company('acme', 'company-a', 'A', 'BUYER')
    await store.grant('acme', 'usr_a', 'company-a')
    objects = EncryptedLocalObjects(tmp_path / 'finance-originals', b'k' * 32)
    client, gate, context, worker = await finance_bridge_client(api, store, objects)
    try:
        result = await gate({'Idempotency-Key': 'actual-import', 'body': {'legal_entity_id': 'company-a', 'files': [file]}}, context)
        assert not result.is_error, result.output
        operation = json.loads(result.output.split('\n', 1)[1])
        assert await worker.run_once()
        persisted = await store.get_operation(FinancePrincipal('acme', 'usr_a'), operation['id'])
        assert persisted['summary']['parsed'] == 1, persisted
        receipts = await store.pool.fetch('SELECT object_key FROM finance_receipt')
        assert len(receipts) == 1 and await objects.get(receipts[0]['object_key']) == sample
        assert all(sample not in path.read_bytes() for path in (tmp_path / 'finance-originals').rglob('*.enc'))
        assert ('/v1/integration-gateway/files/' + file['file_id'], 'usr_a',
                {'revision': '1', 'sha256': file['sha256']}) in seen
        assert any(path == '/v1/integration-gateway/identity' and user == 'usr_a' for path, user, _ in seen)
    finally:
        await client.close()

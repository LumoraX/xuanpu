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

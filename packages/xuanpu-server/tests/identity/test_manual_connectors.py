"""人工网关仅接受有效员工 cookie，并绑定最终 HTTP 请求。"""
from __future__ import annotations

import json
from typing import Any

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_server.config import CallContextConfig
from xuanpu_spec import pack_package, request_fingerprint

PATH = '/v1/integration-actions/support-desk/finance/change'


async def prepare(ident: Any, *, tools: bool = True) -> tuple[Any, list[Any]]:
    """安装真实业务包，用受控传输观察实际报文。"""
    from xuanpu_server.manual_connectors import ManualConnectorService
    from xuanpu_server.runtime.call_context import HumanContextSigner

    platform = ident.world.platform
    key = Ed25519PrivateKey.generate()
    config = CallContextConfig(issuer='platform', keyId='k', privateKeySecret='context-key',
        allowedAudiences={'https://finance.example': ['finance']})
    await platform.vault.put('acme', 'context-key', key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), actor='test')
    await platform.vault.put('acme', 'finance-key', 'PRIVATE_SERVICE_CREDENTIAL', actor='test')
    ident.world.archive()
    root = ident.world.package_dir / "1.0.0-standard-False"
    manifest = yaml.safe_load((root / 'xuanpu.yaml').read_text())
    manifest['metadata']['version'] = '1.1.0'
    manifest['spec']['connectors'] += [{'name': 'finance', 'transport': 'http', 'openapi': 'manual.json',
        'baseUrl': 'https://finance.example', 'contextAudience': 'finance',
        'auth': {'type': 'bearer', 'secret': 'finance-key'},
        'tools': [{'name': 'change', 'effect': 'write'}, {'name': 'read', 'effect': 'read'}] if tools else []}]
    (root / 'xuanpu.yaml').write_text(yaml.safe_dump(manifest))
    (root / 'manual.json').write_text(json.dumps({'openapi': '3.0.3', 'info': {'title': 'test'}, 'paths': {
        '/change': {'post': {'operationId': 'change', 'parameters': [{'name': 'Idempotency-Key', 'in': 'header', 'required': True, 'schema': {'type': 'string'}}],
            'requestBody': {'required': True, 'content': {'application/json': {'schema': {'type': 'object',
                'required': ['amount'], 'additionalProperties': False, 'properties': {'amount': {'type': 'string'}}}}}}}},
        '/read': {'get': {'operationId': 'read'}}, '/hidden': {'get': {'operationId': 'hidden'}}}}))
    await platform.packages.publish(ident.world.admin, pack_package(root), activate=True)
    seen = []
    async def receive(request: httpx.Request) -> httpx.Response:
        """独立验签最终实际 HTTP 请求。"""
        claims = jwt.decode(request.headers['X-Xuanpu-Context'], key.public_key(), algorithms=['EdDSA'],
                            audience='finance', issuer='platform')
        assert claims['origin'] == 'human'
        assert claims['session_id'].startswith('interactive:')
        assert claims['exp'] - claims['iat'] == 60
        body = json.loads(request.content) if request.content else None
        assert claims['request_sha256'] == request_fingerprint(request.method, request.url.path,
            list(request.url.params.multi_items()), body, request.headers.get('Idempotency-Key', ''), request.headers['Content-Type'])
        assert request.headers['authorization'] == 'Bearer PRIVATE_SERVICE_CREDENTIAL'
        seen.append(claims)
        return httpx.Response(201, json={'business_status': 'draft'})
    transport = httpx.MockTransport(receive)
    platform.manual_connectors = ManualConnectorService(packages=platform.packages, vault=platform.vault,
        audit=platform.audit, signer=HumanContextSigner(config=config, vault=platform.vault), transport=transport)
    return transport, seen


async def test_manual_cookie_human_signed_without_model(identity_world: Any, monkeypatch: Any) -> None:
    """模型无法启动时，员工人工路径仍发真实签名并保留结构化结果与脱敏审计。"""
    async with identity_world() as ident:
        transport, seen = await prepare(ident)
        closed = []
        original_close = transport.aclose
        async def close() -> None:
            """观察出口传输的生命周期。"""
            closed.append(True)
            await original_close()
        async def model_unavailable(*args: Any, **kwargs: Any) -> Any:
            """人工路径不得依赖故障的模型组装器。"""
            raise RuntimeError("模型配置不可用")
        monkeypatch.setattr(transport, 'aclose', close)
        monkeypatch.setattr(ident.world.platform.runtimes, 'get', model_unavailable)
        user = await ident.user()
        token = await ident.login(user)
        response = await ident.http.post(PATH, headers={**ident.cookie(token), 'X-Requested-By': 'xuanpu', 'Origin': str(ident.http.base_url).rstrip('/')},
            json={'arguments': {'Idempotency-Key': 'one', 'body': {'amount': 'PRIVATE_AMOUNT'}}, 'confirmed': True})
        assert response.status_code == 200, response.text
        assert response.json() == {'status': 201, 'body': {'business_status': 'draft'}, 'is_error': False, 'truncated': False}
        assert closed == [True]
        assert seen[0]['sub'] == user.user_id
        records = await ident.audit('manual_connector_completed')
        assert records[-1].actor == 'user:' + user.user_id
        assert records[-1].data['session_id'] == seen[0]['session_id']
        serialized = json.dumps([record.data for record in records])
        assert 'PRIVATE_' not in serialized
        assert records[-1].data['origin'] == 'human'


@pytest.mark.parametrize('mode', ['root', 'key', 'act-as', 'cookie-key', 'no-csrf', 'cross-site'])
async def test_manual_gateway_rejects_non_human_or_csrf(identity_world: Any, mode: str) -> None:
    """机器凭据、代员工、混合凭据与跨站请求均无法进入人工网关。"""
    async with identity_world() as ident:
        user = await ident.user()
        headers = {**ident.cookie(await ident.login(user)), 'X-Requested-By': 'xuanpu', 'Origin': str(ident.http.base_url).rstrip('/')}
        if mode in ('root', 'key', 'act-as', 'cookie-key'):
            headers['Authorization'] = 'Bearer ' + (ident.world.root_key if mode == 'root' else ident.world.member_key)
        if mode == 'act-as':
            headers['X-Xuanpu-Act-As'] = user.user_id
        if mode == 'no-csrf':
            del headers['X-Requested-By']
        if mode == 'cross-site':
            headers['Origin'] = 'https://attacker.example'
        response = await ident.http.post(PATH, headers=headers, json={'arguments': {}, 'confirmed': True})
        assert response.status_code in (400, 401, 403), response.text


@pytest.mark.parametrize('mode', ['unconfirmed', 'schema', 'undeclared', 'hidden', 'empty-tools', 'other-package', 'spoof-origin'])
async def test_manual_gateway_declaration_boundaries(identity_world: Any, mode: str) -> None:
    """仅声明白名单内、schema有效且明确确认的操作才发送；客户端不能指定身份或目标。"""
    async with identity_world() as ident:
        _, seen = await prepare(ident, tools=mode != 'empty-tools')
        token = await ident.login(await ident.user())
        arguments: dict[str, Any] = {'Idempotency-Key': 'one', 'body': {'amount': '1.00'}}
        body: dict[str, Any] = {'arguments': arguments, 'confirmed': mode != 'unconfirmed'}
        path = PATH
        if mode == 'schema':
            arguments['body'] = {'amount': 12}
        if mode == 'undeclared':
            arguments['url'] = 'https://attacker.example'
        if mode == 'hidden':
            path = PATH.replace('/change', '/hidden')
        if mode == 'other-package':
            path = PATH.replace('support-desk', 'other-tenant-only')
        if mode == 'spoof-origin':
            body['origin'] = 'human'
        response = await ident.http.post(path, headers={**ident.cookie(token), 'X-Requested-By': 'xuanpu'}, json=body)
        assert response.status_code in (400, 403, 404), response.text
        assert seen == []


async def test_manual_read_needs_no_confirmation(identity_world: Any) -> None:
    """显式声明的只读操作无需业务确认，仍使用人工签名及服务凭据。"""
    async with identity_world() as ident:
        _, seen = await prepare(ident)
        token = await ident.login(await ident.user())
        response = await ident.http.post(PATH.replace('/change', '/read'),
            headers={**ident.cookie(token), 'X-Requested-By': 'xuanpu'}, json={'arguments': {}})
        assert response.status_code == 200
        assert len(seen) == 1


@pytest.mark.parametrize('mode', ['missing-secret', 'wrong-audience', 'disabled-user', 'other-tenant'])
async def test_manual_current_trust_required(identity_world: Any, mode: str) -> None:
    """当前有效员工、同租户安装、服务密钥和操作员目标受众许可缺一不可。"""
    from xuanpu_server.identity import UserRecord
    from xuanpu_server.platform import ROOT_PRINCIPAL

    async with identity_world() as ident:
        _, seen = await prepare(ident)
        user = await ident.user()
        token = await ident.login(user)
        if mode == 'missing-secret':
            assert await ident.world.platform.vault.delete('acme', 'finance-key')
        if mode == 'wrong-audience':
            signer = ident.world.platform.manual_connectors._signer
            assert signer is not None
            signer._config = CallContextConfig(issuer='p', keyId='k', privateKeySecret='context-key', allowedAudiences={'https://other.example': ['finance']})
        if mode == 'disabled-user':
            await ident.store.set_user_status('acme', user.user_id, 'disabled', reason='admin', at=1)
        if mode == 'other-tenant':
            await ident.world.platform.create_tenant(ROOT_PRINCIPAL, tenant_id='other', name='Other')
            other = UserRecord(tenant_id='other', user_id='other-user', display_name='其他员工', created_at=1)
            await ident.store.create_user(other)
            token = await ident.login(other)
        response = await ident.http.post(PATH, headers={**ident.cookie(token), 'X-Requested-By': 'xuanpu'},
            json={'arguments': {'Idempotency-Key': 'one', 'body': {'amount': '1.00'}}, 'confirmed': True})
        assert response.status_code in (400, 401, 403, 404), response.text
        assert seen == []

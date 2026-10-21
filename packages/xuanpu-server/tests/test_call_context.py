"""平台调用上下文的可信会话签发与隔离。"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.controlplane.models import SessionRecord
from xuanpu_server.errors import PermissionDeniedError
from xuanpu_server.runtime.events import SubmissionIndex
from xuanpu_server.secrets import EncryptedVault, generate_master_key
from xuanpu_server.tenancy import Tenant


async def test_signer_uses_persisted_employee_and_rotating_key() -> None:
    """独立验签、跨租户与非员工拒绝、作用域退出和密钥轮换均可验证。"""
    from xuanpu_server.runtime.call_context import bind_call_context
    from xuanpu_server.runtime.events import SessionRef

    env = await signing_environment()
    with bind_call_context(SessionRef('s1', 'forged-user'), 'real-call'):
        token = await sign_test_request(env.signer)
        claims = jwt.decode(token, env.key.public_key(), algorithms=['EdDSA'], audience='finance',
                            issuer='platform', leeway=5)
        assert claims['sub'] == 'u1' and claims['call_id'] == 'real-call'
        assert claims['exp'] - claims['iat'] == 60
        assert claims['kid'] == jwt.get_unverified_header(token)['kid'] == 'key1'
        with pytest.raises(PermissionDeniedError):
            await sign_test_request(env.signer, tenant='other')
    with pytest.raises(PermissionDeniedError):
        await sign_test_request(env.signer)
    await env.store.create_session(SessionRecord(session_id='machine', tenant_id='tenant-a', package='p',
        version='1', entry='a', created_at=time.time(), updated_at=time.time(), created_by='machine'))
    with bind_call_context(SessionRef('machine', None), 'machine-call'):
        with pytest.raises(PermissionDeniedError):
            await sign_test_request(env.signer)
    rotated = Ed25519PrivateKey.generate()
    await save_signing_key(env.vault, 'tenant-a', rotated)
    with bind_call_context(SessionRef('s1', 'forged'), 'rotated-call'):
        token = await sign_test_request(env.signer)
    assert jwt.decode(token, rotated.public_key(), algorithms=['EdDSA'], audience='finance')['call_id'] == 'rotated-call'
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, env.key.public_key(), algorithms=['EdDSA'], audience='finance')


def test_request_builder_rejects_reserved_header() -> None:
    """即便绕过规范加载，直接构造 operation 也无法开放签名头。"""
    from xuanpu_server.runtime.http_api import build_request
    from xuanpu_spec.openapi import Operation, Parameter

    op = Operation(
        "a", "get", "/", "", parameters=(Parameter("X-Xuanpu-Context", "header", False, {}),)
    )
    with pytest.raises(ValueError, match="平台"):
        build_request("https://finance.example", op, {"X-Xuanpu-Context": "fake"})


async def test_http_requires_config_but_old_connectors_work() -> None:
    """未启用上下文的旧 HTTP 调用继续工作，要求签名的新调用必须失败关闭。"""
    import httpx
    from xuanpu_server.runtime.http_api import HttpApiClient
    from xuanpu_spec.openapi import Operation

    store = InMemoryControlPlane()
    vault = EncryptedVault(store, keys={"a": generate_master_key()}, primary="a")

    async def handle(request: httpx.Request) -> httpx.Response:
        """普通旧服务不要求签名。"""
        assert "X-Xuanpu-Context" not in request.headers
        return httpx.Response(200, json={"ok": True})

    plain = HttpApiClient(
        base_url="https://example.com",
        auth=None,
        tenant_id="a",
        vault=vault,
        timeout_seconds=1,
        transport=httpx.MockTransport(handle),
    )
    assert (await plain.call(Operation("a", "get", "/", ""), {}))[1] is False
    await plain.close()
    signed = HttpApiClient(
        base_url="https://example.com",
        auth=None,
        tenant_id="a",
        vault=vault,
        timeout_seconds=1,
        transport=httpx.MockTransport(handle),
        context_audience="finance",
    )
    with pytest.raises(PermissionDeniedError, match="配置"):
        await signed.call(Operation("a", "get", "/", ""), {})
    await signed.close()


async def test_gate_binds_context_and_resets_after_call(world) -> None:
    """工具上下文仅在放行的调用内生效，结束后不得串到后续调用。"""
    import taifeng
    from xuanpu_server.runtime.connectors import ConnectorGate
    from xuanpu_server.runtime.gate_audit import ConnectorToolInfo

    env = await signing_environment(store=world.platform.store, vault=world.platform.vault, tenant='acme')
    index = SubmissionIndex()
    index.register_thread('trusted-thread', 's1', 'forged-user')
    async def invoke(args):
        """在真实闸门内签发。"""
        return await sign_test_request(env.signer, tenant='acme'), False
    gate = ConnectorGate(invoke=invoke, info=ConnectorToolInfo('finance', 'http', 'a', 'read'),
        full_name='api__finance__a', tenant_id='acme', policy=taifeng.PermissionPolicy(default_mode='allow'),
        audit=world.platform.audit, submissions=index, timeout_seconds=2)
    result = await gate({}, taifeng.ToolContext(call_id='real-call', cancel=taifeng.CancellationToken(),
                                              thread_id='trusted-thread'))
    assert not result.is_error
    with pytest.raises(PermissionDeniedError):
        await sign_test_request(env.signer, tenant='acme')


async def test_platform_assembles_optional_signer(world, tmp_path) -> None:
    """部署配置透传到运行时，默认平台不要求签名配置。"""
    from xuanpu_server.config import CallContextConfig

    config = CallContextConfig(
        issuer="p",
        keyId="k",
        privateKeySecret="s",
        allowedAudiences={"https://finance.example": ["finance"]},
    )
    sibling = world.sibling("signing-instance", data_dir=tmp_path / "signing", call_context=config)
    assert sibling.runtimes._context_signer is not None
    assert world.platform.runtimes._context_signer is None
    await sibling.close()


async def test_http_context_binds_actual_request_and_does_not_redirect() -> None:
    """独立接收方按实际HTTP报文验签；重定向不会携带签名跳转。"""
    import json

    import httpx
    from xuanpu_server.runtime.call_context import bind_call_context
    from xuanpu_server.runtime.events import SessionRef
    from xuanpu_server.runtime.http_api import HttpApiClient
    from xuanpu_spec.call_context import request_fingerprint
    from xuanpu_spec.openapi import Operation, Parameter

    env = await signing_environment()
    seen = []
    async def handle(request: httpx.Request) -> httpx.Response:
        """模拟独立财务服务，完全根据收到的请求验签和摘要。"""
        claims = jwt.decode(request.headers['X-Xuanpu-Context'], env.key.public_key(), algorithms=['EdDSA'],
                            audience='finance', issuer='platform', leeway=5)
        assert claims['request_sha256'] == request_fingerprint(request.method, request.url.path,
            list(request.url.params.multi_items()), json.loads(request.content),
            request.headers['Idempotency-Key'], request.headers['Content-Type'])
        assert claims['sub'] == 'u1' and claims['call_id'] == 'c1'
        seen.append(str(request.url))
        return httpx.Response(302, headers={'Location': 'https://attacker.example/stolen'})
    client = HttpApiClient(base_url='https://finance.example/api', auth=None, tenant_id='tenant-a',
        vault=env.vault, timeout_seconds=1, transport=httpx.MockTransport(handle),
        context_audience='finance', context_signer=env.signer)
    operation = Operation('create', 'post', '/expense', '', parameters=(
        Parameter('Idempotency-Key', 'header', True, {}), Parameter('tag', 'query', False, {})), request_body={})
    try:
        with bind_call_context(SessionRef('s1', 'forged'), 'c1'):
            text, error = await client.call(operation, {'Idempotency-Key': 'once', 'tag': ['中', 'a b'],
                                                        'body': {'amount': '1.00'}})
        assert len(seen) == 1 and '302' in text and not error
        for url in ('https://attacker.example', 'https://finance.example:444'):
            with pytest.raises(PermissionDeniedError):
                env.signer.require_audience(url, 'finance')
        with pytest.raises(PermissionDeniedError):
            env.signer.require_audience('https://finance.example', 'other-audience')
    finally:
        await client.close()


def test_request_builder_rejects_duplicate_header_aliases() -> None:
    """绕过加载器构造的头大小写别名同样拒绝，不能让 httpx 合并后改变摘要。"""
    from xuanpu_server.runtime.http_api import build_request
    from xuanpu_spec.openapi import Operation, Parameter

    operation = Operation('a', 'post', '/', '', parameters=(
        Parameter('Idempotency-Key', 'header', False, {}),
        Parameter('idempotency-key', 'header', False, {})))
    with pytest.raises(ValueError, match='重复'):
        build_request('https://finance.example', operation,
                      {'Idempotency-Key': 'one', 'idempotency-key': 'two'})


@pytest.mark.parametrize('header', ['X-Api-Key', 'x-api-key'])
async def test_http_rejects_model_header_colliding_with_credential(header: str) -> None:
    """凭据与模型输入不论大小写都不得覆盖或合并成不同的实际请求。"""
    import httpx
    from xuanpu_server.runtime.http_api import HttpApiClient
    from xuanpu_spec.manifest import HttpAuth
    from xuanpu_spec.openapi import Operation, Parameter

    store = InMemoryControlPlane()
    vault = EncryptedVault(store, keys={'a': generate_master_key()}, primary='a')
    await vault.put('tenant-a', 'secret', 'private', actor='test')
    async def reject(request: httpx.Request) -> httpx.Response:
        """冲突不得抵达外部服务。"""
        pytest.fail('credential collision was sent')
    client = HttpApiClient(base_url='https://finance.example',
        auth=HttpAuth(type='header', secret='secret', header='X-Api-Key'), tenant_id='tenant-a',
        vault=vault, timeout_seconds=1, transport=httpx.MockTransport(reject))
    try:
        result, error = await client.call(Operation('a', 'get', '/', '', parameters=(
            Parameter(header, 'header', True, {}),)), {header: 'model'})
        assert error and '冲突' in result
        assert 'private' not in result
    finally:
        await client.close()


async def test_production_config_bootstrap_signs_two_employee_http_calls(
    world, tmp_path, monkeypatch, step, read_turn
) -> None:
    """生产配置贯穿 bootstrap 与完整运行时，同包并发员工的真实 HTTP JWT 不串身份。"""
    import asyncio
    import base64
    import json

    import httpx
    import xuanpu_server.bootstrap as bootstrap
    from xuanpu_server.config import parse_config
    from xuanpu_server.identity import UserRecord
    from xuanpu_server.platform import ROOT_PRINCIPAL
    from xuanpu_server.runtime.http_api import HttpApiClient
    from xuanpu_spec import pack_package, request_fingerprint

    key = Ed25519PrivateKey.generate()
    seen = []
    async def receiver(request: httpx.Request) -> httpx.Response:
        """仅替换外部网络，接收方独立验签实际报文。"""
        await asyncio.sleep(0)
        claims = jwt.decode(request.headers['X-Xuanpu-Context'], key.public_key(), algorithms=['EdDSA'],
                            issuer='platform', audience='finance', leeway=5)
        assert claims['request_sha256'] == request_fingerprint(request.method, request.url.path,
            list(request.url.params.multi_items()), None, request.headers.get('Idempotency-Key', ''),
            request.headers['Content-Type'])
        seen.append(claims)
        return httpx.Response(200, json={'user': claims['sub']})
    original_init = HttpApiClient.__init__
    def with_network(self, **kwargs):
        """保留生产客户端，只用可检查报文的本机传输替换网络。"""
        original_init(self, **kwargs, transport=httpx.MockTransport(receiver))
    monkeypatch.setattr(HttpApiClient, '__init__', with_network)
    monkeypatch.setattr(bootstrap, 'GatewayModelClientFactory', lambda *args: world.models)
    config = parse_config(json.dumps({'data_dir': str(tmp_path / 'production'), 'root_key_hash': 'a' * 64,
        'master_keys': {'k': base64.b64encode(b'x' * 32).decode()}, 'primary_master_key': 'k',
        'models': {'base_url': 'http://unused-model/v1', 'api_key': 'test',
                   'tiers': {tier: {'model': 'scripted'} for tier in ['fast', 'balanced', 'deep']}},
        'call_context': {'issuer': 'platform', 'keyId': 'production-key', 'privateKeySecret': 'context-key',
                         'allowedAudiences': {'https://finance.example': ['finance']}}}), variables={})
    root = signed_package(tmp_path)
    async with bootstrap.open_platform(config) as platform:
        await platform.start()
        await platform.create_tenant(ROOT_PRINCIPAL, tenant_id='acme', name='Acme')
        await platform.vault.put('acme', 'context-key', key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), actor='test')
        admin = await platform.issue_key(ROOT_PRINCIPAL, tenant_id='acme', name='admin', role='tenant_admin')
        await platform.packages.publish(await platform.authenticate(admin.key), pack_package(root), activate=True)
        credential = await platform.issue_key(ROOT_PRINCIPAL, tenant_id='acme', name='service',
                                               role='tenant_member', act_as_users=True)
        sessions = []
        for user in ('employee-a', 'employee-b'):
            await platform.identity.store.create_user(UserRecord(tenant_id='acme', user_id=user,
                display_name=user, created_at=time.time()))
            who = await platform.authenticate(credential.key, act_as=user)
            session = await platform.sessions.create(who, package='signed-package')
            sessions.append((who, session))
            world.models.script_for(user, step(calls=[('call-' + user, 'api__finance__who', {})]), step(text='完成'))
        async def run(who, session):
            """两名员工并发走完整模型、闸门与签名链路。"""
            events = await read_turn(await platform.sessions.send_message(who, session.session_id, who.user_id))
            assert events[-1].msg.kind == 'turn_completed'
            completed = [event.msg.data for event in events if event.msg.kind == 'tool_call_completed']
            assert completed and all(not item.get('is_error') for item in completed)
        await asyncio.gather(*(run(who, session) for who, session in sessions))
        assert {item['sub'] for item in seen} == {'employee-a', 'employee-b'}
        assert {item['session_id']: item['sub'] for item in seen} == {
            session.session_id: who.user_id for who, session in sessions}
        assert {item['call_id'] for item in seen} == {'call-employee-a', 'call-employee-b'}


async def save_signing_key(vault, tenant, key):
    """将测试密钥写入真实租户密钥库。"""
    await vault.put(tenant, 'signing', key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), actor='test')


async def signing_environment(*, store=None, vault=None, tenant='tenant-a'):
    """建立可复用的真实持久会话、密钥库和Ed25519签名器。"""
    from types import SimpleNamespace

    from xuanpu_server.config import CallContextConfig
    from xuanpu_server.runtime.call_context import CallContextSigner

    if store is None:
        store = InMemoryControlPlane()
        await store.create_tenant(Tenant(tenant, 'A'))
    if vault is None:
        vault = EncryptedVault(store, keys={'a': generate_master_key()}, primary='a')
    key = Ed25519PrivateKey.generate()
    await save_signing_key(vault, tenant, key)
    await store.create_session(SessionRecord(session_id='s1', tenant_id=tenant, package='p', version='1', entry='a',
        created_at=time.time(), updated_at=time.time(), created_by='test', user_id='u1'))
    signer = CallContextSigner(config=CallContextConfig(issuer='platform', keyId='key1', privateKeySecret='signing',
        allowedAudiences={'https://finance.example': ['finance']}), store=store, vault=vault)
    return SimpleNamespace(key=key, store=store, vault=vault, signer=signer)


async def sign_test_request(signer, *, tenant='tenant-a'):
    """固定业务请求，将变化集中在可信主体与作用域。"""
    return await signer.sign(tenant_id=tenant, audience='finance', url='https://finance.example/v1/test',
                             method='POST', query=[], body={})


def signed_package(tmp_path):
    """构造只含平台声明式HTTP连接器的合成业务包。"""
    import json
    root = tmp_path / 'signed-package'
    for name in ('triage', 'faq'):
        (root / 'skills' / name).mkdir(parents=True)
    (root / 'xuanpu.yaml').write_text('''apiVersion: xuanpu/v1
metadata: {name: signed-package, version: 1.0.0}
spec:
  skills: {entries: [triage]}
  connectors:
    - {name: finance, transport: http, openapi: api.json, baseUrl: 'https://finance.example', contextAudience: finance}
''')
    (root / 'api.json').write_text(json.dumps({'openapi': '3.0.3', 'info': {'title': 'finance'},
        'paths': {'/who': {'get': {'operationId': 'who'}}}}))
    (root / 'skills' / 'triage' / 'SKILL.md').write_text('''---
name: triage
description: 身份签名测试
version: 1.0.0
type: composite
entry: true
child_skills: [faq]
tool_names: [api__finance__who]
---
调用指定工具。
''')
    (root / 'skills' / 'faq' / 'SKILL.md').write_text('---\nname: faq\ndescription: 测试\nversion: 1.0.0\ntype: atomic\n---\n正文\n')
    return root

"""共享上下文的来源证明不能在撤权、身份重启或再授权后被旧会话复用。"""
from __future__ import annotations


async def context_for(client, shared_id, request_id, version):
    """原生确认选择共享事项，不从用户文本猜测要打开的内容。"""
    return await client.post('/v1/shared-contexts', json={
        'request_id': request_id, 'confirmed': True, 'expected_version': version,
        'shared_id': shared_id, 'kind': 'assistant_conversation', 'include_content': True,
        'material_revision_ids': []})


async def invite(shared_http, shared_id, request_id, version):
    """明确授予本人只读事项，不提升全局角色。"""
    return await shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/members', json={
        'request_id': request_id, 'confirmed': True, 'expected_version': version,
        'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer'})


async def test_context_is_recorded_before_read_and_revocation_blocks_reuse(shared_http, shared_id):
    """授权下可读，撤销后旧上下文永久阻塞，再授权必须打开新上下文。"""
    grant = await invite(shared_http, shared_id, 'grant-for-context', 1)
    assert grant.status_code == 201, grant.text
    created = await context_for(shared_http['bob'], shared_id, 'context-before-revoke', 2)
    assert created.status_code == 201, created.text
    path = '/v1/shared-contexts/' + created.json()['id']
    read = await shared_http['bob'].get(path + '/read')
    assert read.status_code == 200 and read.json()['content'] == '仅共享事项正文'
    assert read.json()['sources'], '读取前必须有持久来源引用'
    revoked = await shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/members/' + grant.json()['id'] + '/revoke', json={
        'request_id': 'revoke-context-reader', 'confirmed': True, 'expected_version': 2})
    assert revoked.status_code == 200
    denied = await shared_http['bob'].get(path + '/read')
    assert denied.status_code == 403 and denied.json()['error']['code'] == 'shared_access_revoked'
    again = await invite(shared_http, shared_id, 'new-grant-for-context', 3)
    assert again.status_code == 201, again.text
    assert (await shared_http['bob'].get(path + '/read')).status_code == 403
    fresh = await context_for(shared_http['bob'], shared_id, 'fresh-context', 4)
    assert fresh.status_code == 201, fresh.text
    assert (await shared_http['bob'].get('/v1/shared-contexts/' + fresh.json()['id'] + '/read')).status_code == 200


async def test_context_owner_and_tenant_are_not_client_overridable(shared_http, shared_id, r3_beta):
    """管理员、另一员工、机器和同名跨企业账号都不能补读上下文。"""
    created = await context_for(shared_http['alice'], shared_id, 'owner-context', 1)
    assert created.status_code == 201, created.text
    path = '/v1/shared-contexts/' + created.json()['id'] + '/read'
    for client in (shared_http['bob'], shared_http['tenant_admin'], r3_beta['alice']):
        assert (await client.get(path)).status_code == 404
    assert (await shared_http['machine'].get(path)).status_code == 403
    invalid = await shared_http['alice'].post('/v1/shared-contexts', json={
        'request_id': 'override-context-user', 'confirmed': True, 'expected_version': 1,
        'shared_id': shared_id, 'kind': 'assistant_conversation', 'user_id': 'usr_bob'})
    assert invalid.status_code == 400


async def test_context_selection_cannot_add_unreadable_material(shared_http, shared_id):
    """来源选择检查当前精确材料集合，坏选择不会创建空上下文作为回执。"""
    response = await shared_http['alice'].post('/v1/shared-contexts', json={
        'request_id': 'missing-context-material', 'confirmed': True, 'expected_version': 1,
        'shared_id': shared_id, 'kind': 'assistant_conversation', 'include_content': True,
        'material_revision_ids': ['unreadable-revision']})
    assert response.status_code == 400, response.text


async def test_revoked_context_never_reaches_underlying_model(shared_http, shared_id, r3_world, step):
    """检查真实收到的模型请求次数；403本身不能证明旧正文未送往模型。"""
    import pytest
    import taifeng
    from xuanpu_server.errors import SharedAccessRevokedError
    from xuanpu_server.runtime.source_guard import SourceModelClient
    from xuanpu_server.tenancy import Principal
    grant = (await invite(shared_http, shared_id, 'model-reader', 1)).json()
    created = (await context_for(shared_http['bob'], shared_id, 'model-context', 2)).json()
    who = Principal(role='tenant_member', tenant_id='acme', user_id='usr_bob', key_id=None)
    guard = r3_world.platform.shared_contexts
    async def check():
        return await guard.check(who, created['id'])
    inner = await r3_world.models.create('acme', 'fast')
    wrapped = SourceModelClient(inner, check)
    request = taifeng.ApiRequest.model_validate({'model': 'fast', 'input_items': [
        {'type': 'message', 'role': 'user', 'content': '仅共享事项正文'}], 'max_output_tokens': 100})
    r3_world.models.script(step(text='已核对'))
    async with wrapped.session(cancel=taifeng.CancellationToken()) as session:
        async for _ in session.stream(request):
            pass
    before = len(r3_world.models.requests)
    assert before == 1
    revoked = await shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/members/' + grant['id'] + '/revoke', json={
        'request_id': 'revoke-model-reader', 'confirmed': True, 'expected_version': 2})
    assert revoked.status_code == 200
    with pytest.raises(SharedAccessRevokedError):
        async with wrapped.session(cancel=taifeng.CancellationToken()) as session:
            async for _ in session.stream(request):
                pass
    assert len(r3_world.models.requests) == before


async def test_runtime_reads_only_selected_sources_and_blocks_after_revocation(shared_http, shared_id, r3_world, step, read_turn, monkeypatch):
    """公开内核实际请求包含受控读取，撤权后既不补读正文也不再发送模型请求。"""
    from dataclasses import replace

    import pytest
    from xuanpu_server.controlplane.models import AuditQuery
    from xuanpu_server.errors import SharedAccessRevokedError
    from xuanpu_server.tenancy import Principal
    platform = r3_world.platform
    from pathlib import Path

    from xuanpu_spec import pack_package
    root = Path(__file__).resolve().parents[4] / 'examples/shared-reader'
    await platform.packages.publish(r3_world.admin, pack_package(root), activate=True)
    who = Principal(role='tenant_member', tenant_id='acme', user_id='usr_bob', key_id=None)
    grant = (await invite(shared_http, shared_id, 'runtime-reader', 1)).json()
    record = await platform.sessions.create(who, package='shared-reader')
    created = await shared_http['bob'].post('/v1/shared-contexts', json={
        'request_id': 'runtime-source', 'confirmed': True, 'expected_version': 2,
        'shared_id': shared_id, 'kind': 'runtime_session', 'runtime_session_id': record.session_id})
    assert created.status_code == 201, created.text
    from xuanpu_server.errors import PermissionDeniedError
    with pytest.raises(PermissionDeniedError):
        await platform.sessions.content(replace(r3_world.admin, user_id=who.user_id), record.session_id)
    monkeypatch.setattr(platform.runtimes, '_defaults', replace(platform.runtimes.defaults, capture_requests=True))
    r3_world.models.script(step(calls=[('read-source', 'read_shared_context', {})]), step(text='已核对共享事项'))
    await read_turn(await platform.sessions.send_message(who, record.session_id, '读取已选资料并核对'))
    assert len(r3_world.models.requests) == 2
    assert '仅共享事项正文' in r3_world.models.requests[-1].model_dump_json(), r3_world.models.requests[-1].input_items
    assert not r3_world.calls(), '共享阅读模式不启动业务连接器'
    audit = await platform.store.query_audit('acme', AuditQuery(session_id=record.session_id, limit=100))
    assert not any(row.kind == 'llm_request_recorded' for row in audit), '受保护模式不得留下不受来源保护的全文capture'
    before_route = len(r3_world.models.requests)
    routed = await shared_http['bob'].post('/v1/assistant/route', json={
        'text': '把上次回答保存为个人待办', 'session_id': record.session_id, 'request_id': 'no-private-shared-route'})
    assert routed.status_code == 400, routed.text
    assert len(r3_world.models.requests) == before_route
    revoked = await shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/members/' + grant['id'] + '/revoke', json={
        'request_id': 'runtime-revoke', 'confirmed': True, 'expected_version': 2})
    assert revoked.status_code == 200
    before = len(r3_world.models.requests)
    with pytest.raises(SharedAccessRevokedError):
        await platform.sessions.content(who, record.session_id)
    with pytest.raises(SharedAccessRevokedError):
        await platform.sessions.send_message(who, record.session_id, '继续旧资料')
    assert len(r3_world.models.requests) == before

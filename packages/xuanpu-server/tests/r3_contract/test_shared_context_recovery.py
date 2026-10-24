"""重新装配平台与引擎回收后仍使用持久来源，撤权不靠内存缓存。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from xuanpu_server.assistant import RouteBody
from xuanpu_server.errors import SharedAccessRevokedError
from xuanpu_server.tenancy import Principal
from xuanpu_spec import pack_package

from .test_shared_context import context_for, invite


def bob():
    """身份由测试夹具预配，不从姓名或可见范围推断。"""
    return Principal(role='tenant_member', tenant_id='acme', user_id='usr_bob', key_id=None)


async def test_revoked_runtime_is_evicted_and_cold_history_stays_blocked(shared_http, shared_id, r3_world, step, read_turn):
    """维护回收引擎及缓存；全新平台不能通过历史、重放或续跑取回旧正文。"""
    platform = r3_world.platform
    root = Path(__file__).resolve().parents[4] / 'examples/shared-reader'
    await platform.packages.publish(r3_world.admin, pack_package(root), activate=True)
    grant = (await invite(shared_http, shared_id, 'recovery-reader', 1)).json()
    record = await platform.sessions.create(bob(), package='shared-reader')
    response = await shared_http['bob'].post('/v1/shared-contexts', json={
        'request_id': 'recovery-source', 'confirmed': True, 'expected_version': 2,
        'shared_id': shared_id, 'kind': 'runtime_session', 'runtime_session_id': record.session_id})
    assert response.status_code == 201, response.text
    r3_world.models.script(step(calls=[('selected', 'read_shared_context', {})]), step(text='受保护正文摘要'))
    turn = await platform.sessions.send_message(bob(), record.session_id, '核对来源')
    await read_turn(turn)
    assert platform.runtimes.live_runtimes() == 1
    response = await shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/members/' + grant['id'] + '/revoke', json={
        'request_id': 'recovery-revoke', 'confirmed': True, 'expected_version': 2})
    assert response.status_code == 200, response.text
    await platform.sessions.maintain()
    assert platform.runtimes.live_runtimes() == 0
    before = len(r3_world.models.requests)
    await platform.close()
    restarted = r3_world.sibling('shared-restart', data_dir=r3_world.data_dir)
    await restarted.start()
    try:
        with pytest.raises(SharedAccessRevokedError):
            await restarted.sessions.content(bob(), record.session_id)
        with pytest.raises(SharedAccessRevokedError):
            await restarted.sessions.replay(bob(), record.session_id, turn.submission_id)
        with pytest.raises(SharedAccessRevokedError):
            await restarted.sessions.send_message(bob(), record.session_id, '继续')
        assert len(r3_world.models.requests) == before
    finally:
        await restarted.close()


async def test_protected_main_history_and_cache_cold_recovery(shared_http, shared_id, r3_world, step):
    """先停止平台再撤权，冷平台首次补读即拒绝，无需旧内存失效事件。"""
    platform = r3_world.platform
    grant = (await invite(shared_http, shared_id, 'cold-main-reader', 1)).json()
    context = (await context_for(shared_http['bob'], shared_id, 'cold-main-source', 2)).json()
    body = RouteBody(text='核对来源', request_id='cold-main-chat', shared_context_id=context['id'])
    r3_world.models.script(step(text=json.dumps({'message': '冷恢复共享机密'})))
    assert (await platform.assistant.route(bob(), body)).message == '冷恢复共享机密'
    await platform.close()
    restarted = r3_world.sibling('main-restart', data_dir=r3_world.data_dir)
    await restarted.start()
    try:
        # 关新功能不影响既有受保护读取和撤销，不能靠关闭开关掩盖回归。
        assert (await restarted.assistant.route(bob(), body)).message == '冷恢复共享机密'
        from xuanpu_server.shared_matters.schemas import SharedChange
        alice = Principal(role='tenant_member', tenant_id='acme', user_id='usr_alice', key_id=None)
        await restarted.shared_matters.members.revoke(alice, shared_id, grant['id'], SharedChange(
            request_id='cold-revoke', confirmed=True, expected_version=2))
        before = len(r3_world.models.requests)
        with pytest.raises(SharedAccessRevokedError):
            await restarted.assistant.route(bob(), body)
        history = await restarted.workspace.messages(bob())
        assert '冷恢复共享机密' not in json.dumps(history, ensure_ascii=False)
        assert len(r3_world.models.requests) == before
    finally:
        await restarted.close()


@pytest.mark.parametrize('endpoint', ['history', 'events', 'pending'])
async def test_audit_read_rechecks_sources_before_return(shared_http, shared_id, r3_world, step, read_turn, monkeypatch, endpoint):
    """共享审计查询已读到正文时撤权，JSON补读也不能把旧内容返回。"""
    platform = r3_world.platform
    root = Path(__file__).resolve().parents[4] / 'examples/shared-reader'
    await platform.packages.publish(r3_world.admin, pack_package(root), activate=True)
    grant = (await invite(shared_http, shared_id, 'audit-race-reader', 1)).json()
    record = await platform.sessions.create(bob(), package='shared-reader')
    created = await shared_http['bob'].post('/v1/shared-contexts', json={
        'request_id': 'audit-race-source', 'confirmed': True, 'expected_version': 2,
        'shared_id': shared_id, 'kind': 'runtime_session', 'runtime_session_id': record.session_id})
    assert created.status_code == 201, created.text
    r3_world.models.script(step(calls=[('audit-selected', 'read_shared_context', {})]), step(text='不可返回的共享审计正文'))
    turn = await platform.sessions.send_message(bob(), record.session_id, '核对')
    await read_turn(turn)
    original = platform.store.query_audit
    revoked = False
    async def query(*args, **kwargs):
        """实际读取后在返回调用者前变更持久授权。"""
        nonlocal revoked
        rows = await original(*args, **kwargs)
        if not revoked:
            revoked = True
            response = await shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/members/' + grant['id'] + '/revoke', json={
                'request_id': 'audit-race-revoke', 'confirmed': True, 'expected_version': 2})
            assert response.status_code == 200, response.text
        return rows
    monkeypatch.setattr(platform.store, 'query_audit', query)
    url = '/v1/sessions/' + record.session_id + '/' + endpoint
    response = await shared_http['bob'].get(url, params={'submission_id': turn.submission_id} if endpoint == 'events' else None)
    assert revoked
    assert response.status_code == 403, response.text
    assert '不可返回的共享审计正文' not in response.text


async def test_late_source_binding_stops_ordinary_sample_before_actual_send(shared_http, shared_id, r3_world, step, monkeypatch):
    """普通路由入口通过后才绑定来源，发送边界仍识别模式改变并拒绝。"""
    platform = r3_world.platform
    root = Path(__file__).resolve().parents[4] / 'examples/shared-reader'
    await platform.packages.publish(r3_world.admin, pack_package(root), activate=True)
    await invite(shared_http, shared_id, 'late-source-reader', 1)
    record = await platform.sessions.create(bob(), package='shared-reader')
    original = platform.assistant._sample
    async def sample(*args, **kwargs):
        """在正常历史装配完成后、底层模型stream前绑定来源。"""
        created = await shared_http['bob'].post('/v1/shared-contexts', json={
            'request_id': 'late-source', 'confirmed': True, 'expected_version': 2,
            'shared_id': shared_id, 'kind': 'runtime_session', 'runtime_session_id': record.session_id})
        assert created.status_code == 201, created.text
        return await original(*args, **kwargs)
    monkeypatch.setattr(platform.assistant, '_sample', sample)
    r3_world.models.script(step(text=json.dumps({'package': None, 'entry': None, 'message': '不应发送'})))
    response = await shared_http['bob'].post('/v1/assistant/route', json={
        'text': '继续', 'request_id': 'late-private-route', 'session_id': record.session_id})
    assert response.status_code == 400, response.text
    assert not r3_world.models.requests

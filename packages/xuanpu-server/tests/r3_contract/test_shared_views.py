"""到人共享的原生可用动作、限定资料选择和有来源历史分页。"""
from __future__ import annotations

import json

from .test_shared_context import context_for, invite


async def test_packet_context_uses_its_own_visible_version(shared_http, shared_id):
    """限定读者能原生选择资料，无需枚举原事项版本，原正文更新不改写资料包。"""
    path = '/v1/shared-matters/' + shared_id
    granted = await shared_http['alice'].post(path + '/members', json={
        'request_id': 'packet-native', 'confirmed': True, 'expected_version': 1,
        'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer', 'scope': 'packet',
        'packet_title': '本人限定资料', 'include_content': True})
    assert granted.status_code == 201, granted.text
    visible = (await shared_http['bob'].get(path)).json()
    assert isinstance(visible['version'], int) and visible['version'] > 0
    assert visible['allowed_actions'] == ['read_materials', 'start_context']
    context = await context_for(shared_http['bob'], shared_id, 'packet-native-context', visible['version'])
    assert context.status_code == 201, context.text
    changed = await shared_http['alice'].patch(path, json={
        'request_id': 'unrelated-private-change', 'confirmed': True, 'expected_version': 2,
        'title': '资料包外的新名称', 'content': '资料包外的新正文'})
    assert changed.status_code == 200, changed.text
    current = (await shared_http['bob'].get(path)).json()
    assert current['version'] == visible['version'] and '资料包外' not in json.dumps(current, ensure_ascii=False)
    read = await shared_http['bob'].get('/v1/shared-contexts/' + context.json()['id'] + '/read')
    assert read.json()['content'] == '仅共享事项正文'


async def test_native_actions_are_role_and_feature_bound(shared_http, shared_id, r3_world):
    """员工页面使用服务端动作，不将管理员或viewer渲染成编辑者。"""
    grant = await invite(shared_http, shared_id, 'native-viewer', 1)
    assert grant.status_code == 201
    path = '/v1/shared-matters/' + shared_id
    owner = (await shared_http['alice'].get(path)).json()
    viewer = (await shared_http['bob'].get(path)).json()
    assert {'edit', 'publish_material', 'manage_members', 'transfer_owner'} <= set(owner['allowed_actions'])
    assert 'edit' not in viewer['allowed_actions'] and 'publish_material' not in viewer['allowed_actions']
    assert (await shared_http['tenant_admin'].get(path)).status_code == 404
    from xuanpu_server.shared_matters.models import SharedMatterConfig
    r3_world.platform.shared_matters.config = SharedMatterConfig()
    closed = (await shared_http['alice'].get(path)).json()
    assert 'start_context' not in closed['allowed_actions'] and 'publish_material' not in closed['allowed_actions']
    assert 'revoke_member' in closed['allowed_actions']
    assert (await shared_http['alice'].get('/v1/shared-matters')).json()['enabled'] is False


async def test_own_context_close_is_terminal_and_retries_stay_stable(shared_http, shared_id):
    """结束阅读保留来源历史，旧上下文不可复活，重新阅读须创建新上下文。"""
    created = await context_for(shared_http['alice'], shared_id, 'close-native-source', 1)
    assert created.status_code == 201
    path = '/v1/shared-contexts/' + created.json()['id']
    metadata = await shared_http['alice'].get(path)
    assert metadata.status_code == 200 and metadata.json()['version'] == 1
    body = {'request_id': 'close-source', 'confirmed': True, 'expected_version': 1}
    first = await shared_http['alice'].post(path + '/close', json=body)
    again = await shared_http['alice'].post(path + '/close', json=body)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert (await shared_http['alice'].get(path + '/read')).status_code == 403
    assert (await shared_http['bob'].get(path)).status_code == 404
    assert (await context_for(shared_http['alice'], shared_id, 'new-after-close', 1)).status_code == 201
    mine = (await shared_http['alice'].get('/v1/shared-contexts', params={'limit': 1})).json()
    assert mine['total'] == 2 and len(mine['items']) == 1
    assert (await shared_http['bob'].get('/v1/shared-contexts')).json()['total'] == 0


async def test_context_and_private_history_filters_apply_before_pagination(shared_http, shared_id, r3_world, step):
    """共享对话不会挤掉普通对话分页，跨员工选择上下文仍拒绝。"""
    created = (await context_for(shared_http['alice'], shared_id, 'history-filter-context', 1)).json()
    r3_world.models.script(step(text=json.dumps({'message': '共享答复'})))
    response = await shared_http['alice'].post('/v1/assistant/route', json={
        'request_id': 'filter-shared-chat', 'text': '共享提问', 'shared_context_id': created['id']})
    assert response.status_code == 200, response.text
    r3_world.models.script(step(text=json.dumps({'package': None, 'entry': None, 'message': '私人答复'})))
    assert (await shared_http['alice'].post('/v1/assistant/route', json={
        'request_id': 'filter-private-chat', 'text': '私人提问'})).status_code == 200
    params = {'source_context_id': created['id'], 'latest': 'true', 'limit': 1}
    source = await shared_http['alice'].get('/v1/workspace/messages', params=params)
    assert source.status_code == 200 and source.json()['items'][0]['text'] == '共享答复'
    personal = await shared_http['alice'].get('/v1/workspace/messages', params={'private_only': 'true', 'limit': 1})
    assert personal.json()['items'][0]['text'] == '私人提问'
    assert (await shared_http['bob'].get('/v1/workspace/messages', params=params)).status_code == 404

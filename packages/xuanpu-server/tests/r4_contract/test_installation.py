"""预检、明确确认及安装CAS，始终不产生可运行发布。"""
from __future__ import annotations

import copy

import pytest


async def register(client, ids):
    """经真实产品接口登记模板。"""
    result = await client.post('/v1/capabilities', json={'request_id': 'register', 'delivery': ids['delivery']})
    assert result.status_code == 201, result.text
    return result.json()['capability_id']


async def test_preview_install_and_state_are_separate(r4_http, r4_ids):
    """预检不安装，安装只staged，ready必须有后续发布证据。"""
    client = r4_http['admin']
    cap = await register(client, r4_ids)
    preview = await client.post(f'/v1/capabilities/{cap}/install-previews', json={
        'request_id': 'preview', 'revision': 1})
    assert preview.status_code == 201, preview.text
    assert preview.json()['can_install'] is True
    assert preview.json()['expected_version'] == 0
    body = {'request_id': 'install', 'preview_id': preview.json()['id'],
            'expected_version': 0, 'confirmed': True}
    installed = await client.post(f'/v1/capabilities/{cap}/installations', json=body)
    assert installed.status_code == 201, installed.text
    assert installed.json()['status'] == 'staged'
    assert installed.json()['can_run'] is False
    assert (await client.post(f'/v1/capabilities/{cap}/installations', json=body)).json() == installed.json()
    identifier = installed.json()['id']
    for key, state, expected in [('ready', 'ready', 409), ('stop', 'disabled', 200)]:
        result = await client.post(f'/v1/installations/{identifier}/state', json={
            'request_id': key, 'expected_version': 1, 'status': state})
        assert result.status_code == expected, result.text


async def test_preview_foreign_id_unknown_config_and_plaintext(r4_http, r4_ids):
    """异租户不可枚举；未声明配置不能进入预检或泄漏值。"""
    cap = await register(r4_http['admin'], r4_ids)
    response = await r4_http['outsider'].post(f'/v1/capabilities/{cap}/install-previews',
        json={'request_id': 'foreign', 'revision': 1})
    assert response.status_code == 404, response.text
    response = await r4_http['admin'].post(f'/v1/capabilities/{cap}/install-previews',
        json={'request_id': 'unknown', 'revision': 1, 'public_config': {'unknown': 'PRIVATE-MARKER'}})
    assert response.status_code == 422, response.text
    assert 'PRIVATE-MARKER' not in response.text


async def test_preview_expiry_and_disabled_switch(r4_http, r4_ids, r4_world):
    """过期预检或关闭新受理后，旧确认不能安装。"""
    from xuanpu_server.capabilities.config import CapabilityDeliveryConfig
    cap = await register(r4_http['admin'], r4_ids)
    response = await r4_http['admin'].post(f'/v1/capabilities/{cap}/install-previews',
        json={'request_id': 'expires', 'revision': 1})
    assert response.status_code == 201, response.text
    service = r4_world.platform.capabilities
    now = service.now
    service.now = lambda: now() + 901
    body = {'request_id': 'install-expired', 'preview_id': response.json()['id'],
            'expected_version': 0, 'confirmed': True}
    response = await r4_http['admin'].post(f'/v1/capabilities/{cap}/installations', json=body)
    assert response.status_code == 409, response.text
    service.now = now
    service.config = CapabilityDeliveryConfig(enabled=True, tenants=())
    response = await r4_http['admin'].post(f'/v1/capabilities/{cap}/install-previews',
        json={'request_id': 'disabled', 'revision': 1})
    assert response.status_code == 403
    assert (await r4_http['admin'].get('/v1/capabilities')).status_code == 200


async def test_install_requires_current_inputs_and_new_config_revision(r4_http, r4_ids):
    """新配置追加修订；预检后的旧expected_version不能覆盖。"""
    client = r4_http['admin']
    cap = await register(client, r4_ids)
    previews = []
    for index in range(2):
        result = await client.post(f'/v1/capabilities/{cap}/install-previews',
            json={'request_id': f'p-{index}', 'revision': 1})
        assert result.status_code == 201, result.text
        previews.append(result.json()['id'])
    first = await client.post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': 'first', 'preview_id': previews[0], 'expected_version': 0, 'confirmed': True})
    assert first.status_code == 201, first.text
    stale = await client.post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': 'stale', 'preview_id': previews[1], 'expected_version': 0, 'confirmed': True})
    assert stale.status_code == 409, stale.text
    latest = await client.post(f'/v1/capabilities/{cap}/install-previews',
        json={'request_id': 'latest', 'revision': 1})
    second = await client.post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': 'second', 'preview_id': latest.json()['id'], 'expected_version': 1, 'confirmed': True})
    assert second.status_code == 201, second.text
    assert second.json()['id'] == first.json()['id']
    assert second.json()['config_revision'] == 2


@pytest.mark.parametrize('confirmed', [False, 'true', 1])
async def test_confirmation_is_literal_boolean(r4_http, r4_ids, confirmed):
    """不能用字符串或数字代替本人明确确认。"""
    cap = await register(r4_http['admin'], r4_ids)
    result = await r4_http['admin'].post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': 'unconfirmed', 'preview_id': 'unknown', 'expected_version': 0, 'confirmed': confirmed})
    assert result.status_code == 422, result.text


async def test_registered_revision_never_changes_active_version(r4_http, r4_ids, r4_world):
    """已发布的旧包仍按原激活规则运行，登记不会纳管或自动关闭。"""
    before = await r4_world.platform.packages.active_version('acme', 'office-review')
    body = copy.deepcopy(r4_ids)
    await register(r4_http['admin'], body)
    assert await r4_world.platform.packages.active_version('acme', 'office-review') == before

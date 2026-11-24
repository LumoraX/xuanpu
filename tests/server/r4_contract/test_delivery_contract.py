"""交付描述只声明需求，不自动安装执行或扩展员工权力。"""
from __future__ import annotations

import copy

import pytest


async def test_member_cannot_register_delivery(r4_http, r4_ids):
    """普通成员不能通过交付描述赋权。"""
    response = await r4_http['alice'].post('/v1/capabilities', json={
        'request_id': 'register-denied', 'delivery': r4_ids['delivery']})
    assert response.status_code == 403


async def test_registration_does_not_grant_execution(r4_http, r4_ids):
    """登记后本人目录为空，原包激活行为保持兼容。"""
    response = await r4_http['admin'].post('/v1/capabilities', json={
        'request_id': 'register-one', 'delivery': r4_ids['delivery']})
    assert response.status_code == 201, response.text
    view = await r4_http['alice'].get('/v1/me/capabilities')
    assert view.status_code == 200
    assert view.json()['capabilities'] == []
    assert (await r4_http['machine'].get('/v1/me/capabilities')).status_code == 403


@pytest.mark.parametrize('field,value', [('tenant_id', 'beta'), ('secret', 'plaintext'),
                                        ('actor', 'usr_other')])
async def test_delivery_rejects_identity_and_secrets(r4_http, r4_ids, field, value):
    """模板不是秘密或员工身份的传递通道。"""
    body = copy.deepcopy(r4_ids['delivery'])
    body[field] = value
    response = await r4_http['admin'].post('/v1/capabilities', json={
        'request_id': 'invalid-template', 'delivery': body})
    assert response.status_code == 422, response.text
    assert value not in response.text


async def test_delivery_must_match_archive_and_entry(r4_http, r4_ids):
    """合法描述也必须与已发布不可变包相符。"""
    for field, value in [('digest', 'sha256:' + '0' * 64), ('entry', 'missing')]:
        body = copy.deepcopy(r4_ids['delivery'])
        body['package'][field] = value
        response = await r4_http['admin'].post('/v1/capabilities', json={
            'request_id': 'bad-' + field, 'delivery': body})
        assert response.status_code == 409, response.text


async def test_delivery_rejects_permission_underdeclaration(r4_http, r4_ids):
    """描述不能把实际预算、审批或模型档位改成另一种权限。"""
    body = copy.deepcopy(r4_ids['delivery'])
    body['permissions']['approval'] = 'relaxed'
    response = await r4_http['admin'].post('/v1/capabilities', json={
        'request_id': 'bad-policy', 'delivery': body})
    assert response.status_code == 409, response.text


async def test_registration_replay_and_new_revision(r4_http, r4_ids):
    """同操作回放；同键异内容冲突；新描述追加修订。"""
    body = {'request_id': 'register', 'delivery': r4_ids['delivery']}
    first = await r4_http['admin'].post('/v1/capabilities', json=body)
    assert first.status_code == 201, first.text
    again = await r4_http['admin'].post('/v1/capabilities', json=body)
    assert again.json() == first.json()
    modified = copy.deepcopy(body)
    modified['delivery']['title'] = '新说明'
    assert (await r4_http['admin'].post('/v1/capabilities', json=modified)).status_code == 409
    modified['request_id'] = 'revision-two'
    second = await r4_http['admin'].post('/v1/capabilities', json=modified)
    assert second.status_code == 201, second.text
    assert second.json()['capability_id'] == first.json()['capability_id']
    assert second.json()['revision'] == 2

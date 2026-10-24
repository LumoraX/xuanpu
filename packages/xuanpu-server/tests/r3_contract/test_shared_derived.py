"""受控文本草稿发布保留来源，不通过个人文件洗掉原始权限。"""
from __future__ import annotations

import json

from .test_shared_context import context_for, invite


async def draft_for(shared_http, shared_id, r3_world, step):
    """Bob只读原事项，并在本人受保护主对话产生草稿。"""
    grant = (await invite(shared_http, shared_id, 'derived-reader', 1)).json()
    context = (await context_for(shared_http['bob'], shared_id, 'derived-context', 2)).json()
    r3_world.models.script(step(text=json.dumps({'message': '固定共享文本草稿'})))
    response = await shared_http['bob'].post('/v1/assistant/route', json={
        'request_id': 'derived-chat', 'text': '起草摘要', 'shared_context_id': context['id']})
    assert response.status_code == 200, response.text
    rows = (await shared_http['bob'].get('/v1/workspace/messages')).json()['items']
    target = await shared_http['bob'].post('/v1/shared-matters', json={
        'request_id': 'derived-target', 'confirmed': True, 'title': '本人另建共享事项'})
    assert target.status_code == 201, target.text
    return grant, context, rows[-1], target.json()


async def test_confirmed_text_draft_keeps_source_and_blocks_unrelated_recipient(shared_http, shared_id, r3_world, step):
    """目标事项成员不自动获得原事项授权；原来源撤权后派生副本仍拒绝读取。"""
    grant, context, message, target = await draft_for(shared_http, shared_id, r3_world, step)
    target_id = target['id']
    body = {'request_id': 'publish-shared-draft', 'confirmed': True, 'expected_version': 1,
        'source_context_id': context['id'], 'draft_message_id': message['id'], 'name': '共享草稿.txt'}
    response = await shared_http['bob'].post('/v1/shared-matters/' + target_id + '/material-texts', json=body)
    assert response.status_code == 202, response.text
    publication = response.json()
    listing = (await shared_http['bob'].get('/v1/shared-matters/' + target_id + '/materials')).json()['items']
    material = listing[0]
    url = '/v1/shared-matters/' + target_id + '/materials/' + material['id'] + '/content?revision=1'
    assert (await shared_http['bob'].get(url)).content == '固定共享文本草稿'.encode()
    receipt = await shared_http['bob'].post('/v1/shared-matters/' + target_id + '/material-texts', json=body)
    assert receipt.json() == publication
    # 管理员被明确邀请到目标事项，仍未获得原来源事项的内容权限。
    invited = await shared_http['bob'].post('/v1/shared-matters/' + target_id + '/members', json={
        'request_id': 'derived-target-reader', 'confirmed': True, 'expected_version': 3,
        'subject_kind': 'user', 'subject_id': 'usr_tenant_admin', 'role': 'viewer'})
    assert invited.status_code == 201, invited.text
    assert (await shared_http['tenant_admin'].get(url)).status_code == 404
    listing = (await shared_http['tenant_admin'].get('/v1/shared-matters/' + target_id + '/materials')).json()
    assert listing['total'] == 0
    revoked = await shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/members/' + grant['id'] + '/revoke', json={
        'request_id': 'derived-source-revoke', 'confirmed': True, 'expected_version': 2})
    assert revoked.status_code == 200
    assert (await shared_http['bob'].get(url)).status_code == 404
    assert (await shared_http['bob'].post('/v1/shared-matters/' + target_id + '/material-texts', json=body)).status_code == 403


async def test_draft_publication_does_not_accept_client_text_or_other_message(shared_http, shared_id, r3_world, step):
    """模型和客户端不能伪造来源正文或借他人消息发布；只接本人明确选定的固定草稿。"""
    _, context, message, target = await draft_for(shared_http, shared_id, r3_world, step)
    body = {'request_id': 'bad-draft', 'confirmed': True, 'expected_version': 1,
        'source_context_id': context['id'], 'draft_message_id': message['id'], 'name': '草稿.txt'}
    url = '/v1/shared-matters/' + target['id'] + '/material-texts'
    assert (await shared_http['bob'].post(url, json={**body, 'text': '伪造正文'})).status_code == 400
    assert (await shared_http['bob'].post(url, json={**body, 'draft_message_id': 1})).status_code == 400
    assert (await shared_http['alice'].post(url, json=body)).status_code == 404

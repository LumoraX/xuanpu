"""无需部门的唯一真人受理与精确资料授权，所有动作通过正式HTTP。"""
from __future__ import annotations

import asyncio

import pytest


async def submit(http, shared_id, request='handoff-create', **extra):
    """事项owner明确选择接收人及输入，不由模型或可见范围猜测。"""
    current = (await http['alice'].get('/v1/shared-matters/' + shared_id)).json()
    return await http['alice'].post('/v1/handoffs', json={'request_id': request, 'confirmed': True,
        'expected_version': current['version'], 'shared_id': shared_id, 'target_kind': 'user',
        'target_id': 'usr_bob', 'title': '仅核对指定材料', 'summary': '请核对明确的资料包', **extra})


async def act(client, identifier, action, version, request, **extra):
    """每次动作固定交接版本与本人确认。"""
    return await client.post('/v1/handoffs/' + identifier + '/' + action, json={
        'request_id': request, 'confirmed': True, 'expected_version': version, **extra})


async def test_named_target_can_receive_without_unit_and_cannot_take_full_matter(handoff_http, shared_id):
    """接收前只读来件摘要，接受后只获本次packet，不能编辑整事项。"""
    http = handoff_http
    response = await submit(http, shared_id, include_content=True)
    assert response.status_code == 201, response.text
    identifier = response.json()['id']
    path = '/v1/handoffs/' + identifier
    queued = await http['bob'].get(path)
    assert queued.status_code == 200 and queued.json()['summary'] == '请核对明确的资料包'
    assert not queued.json().get('content_snapshot')
    assert (await http['bob'].get('/v1/shared-matters/' + shared_id)).status_code == 404
    assert (await http['tenant_admin'].get(path)).status_code == 404
    claimed = await act(http['bob'], identifier, 'accept', 1, 'accept-explicit-target')
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()['assignee_id'] == 'usr_bob'
    visible = (await http['bob'].get('/v1/shared-matters/' + shared_id)).json()
    assert visible['scope'] == 'packet' and visible['title'] == '仅核对指定材料'
    assert visible['content'] == '仅共享事项正文' and 'owner_id' not in visible
    edited = await http['bob'].patch('/v1/shared-matters/' + shared_id, json={
        'request_id': 'not-editor', 'confirmed': True, 'expected_version': 2, 'title': '不能接管'})
    assert edited.status_code == 404
    replay = await act(http['bob'], identifier, 'accept', 1, 'accept-explicit-target')
    assert replay.json() == claimed.json()


async def test_two_accepts_have_one_assignee_and_one_receipt(handoff_http, shared_id):
    """不同操作号竞争同一版本只有一项成功，不重复创建责任人。"""
    created = await submit(handoff_http, shared_id)
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    results = await asyncio.gather(*(act(handoff_http['bob'], identifier, 'accept', 1, 'accept-' + str(i)) for i in range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    current = (await handoff_http['alice'].get('/v1/handoffs/' + identifier)).json()
    assert current['status'] == 'accepted' and current['assignee_id'] == 'usr_bob'
    assert len([e for e in current['history'] if e['action'] == 'accept']) == 1


async def test_requester_cannot_accept_for_target_or_self_sign_before_delivery(handoff_http, shared_id):
    """发起、受理、交付和签收各自鉴权，缺成果不能直接签收。"""
    created = await submit(handoff_http, shared_id)
    assert created.status_code == 201
    identifier = created.json()['id']
    assert (await act(handoff_http['alice'], identifier, 'accept', 1, 'owner-cannot-accept')).status_code == 403
    assert (await act(handoff_http['alice'], identifier, 'sign-off', 1, 'early-sign', delivery_version=1)).status_code == 409
    assert (await act(handoff_http['bob'], identifier, 'start', 1, 'early-start')).status_code == 409


async def test_received_queue_and_identity_override_are_scoped(handoff_http, shared_id):
    """目标本人查看received，管理员不能借队列枚举内容或覆盖当前员工。"""
    created = await submit(handoff_http, shared_id)
    assert created.status_code == 201
    mine = (await handoff_http['bob'].get('/v1/handoffs', params={'view': 'received', 'limit': 1})).json()
    assert mine['total'] == 1 and len(mine['items']) == 1
    hidden = (await handoff_http['tenant_admin'].get('/v1/handoffs', params={'view': 'received'})).json()
    assert hidden['total'] == 0
    override = await handoff_http['bob'].get('/v1/handoffs', params={'view': 'received', 'user_id': 'usr_alice'})
    assert override.status_code == 400


@pytest.mark.parametrize('status', ['closed', 'archived'])
async def test_ended_source_cannot_start_new_handoff(handoff_http, shared_id, status):
    """已结束来源不能创建无法发布成果的新交接，保留既有历史查询。"""
    path = '/v1/shared-matters/' + shared_id
    changed = await handoff_http['alice'].patch(path, json={
        'request_id': 'end-source', 'confirmed': True, 'expected_version': 1, 'status': status})
    assert changed.status_code == 200, changed.text
    denied = await submit(handoff_http, shared_id)
    assert denied.status_code == 409, denied.text
    assert (await handoff_http['alice'].get('/v1/handoffs', params={'view': 'sent'})).json()['total'] == 0

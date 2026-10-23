"""共享权属、跨企业隔离和并发错误不得产生半提交授权。"""
from __future__ import annotations

import asyncio

import pytest
from xuanpu_server.errors import ConflictError


async def test_shared_cross_tenant_ids_do_not_authorize(shared_http, shared_id, r3_beta):
    """另一企业同名同ID员工和管理员都不能读取、搜索或改写。"""
    path = '/v1/shared-matters/' + shared_id
    for client in r3_beta.values():
        for suffix in ('', '/members', '/events'):
            assert (await client.get(path + suffix)).status_code == 404
        assert (await client.get('/v1/shared-matters')).json()['total'] == 0
        assert (await client.patch(path, json={
            'request_id': 'cross-tenant', 'confirmed': True, 'expected_version': 1,
            'title': '不能改写'})).status_code == 404


async def test_shared_owner_transfer_race_has_one_winner(shared_http, shared_id):
    """两个明确转交竞争同一版本，只有一份权属和一份变更历史。"""
    path = '/v1/shared-matters/' + shared_id
    async def transfer(target):
        return await shared_http['alice'].post(path + '/transfer-owner', json={
            'request_id': 'transfer-to-' + target, 'confirmed': True,
            'expected_version': 1, 'user_id': 'usr_' + target})
    responses = await asyncio.gather(transfer('bob'), transfer('tenant_admin'))
    assert sum(r.status_code == 200 for r in responses) == 1
    assert all(r.status_code in (200, 404, 409) for r in responses)
    winner = 'bob' if responses[0].status_code == 200 else 'tenant_admin'
    members = (await shared_http[winner].get(path + '/members')).json()['items']
    assert len([m for m in members if m['status'] == 'active' and m['role'] == 'owner']) == 1
    replay = await transfer(winner)
    assert replay.status_code == 200 and replay.json() == responses[0 if winner == 'bob' else 1].json()
    assert (await shared_http['alice'].get(path)).status_code == 404


async def test_shared_bad_grant_rolls_back_everything(shared_http, shared_id, r3_world):
    """坏材料范围不留下成员、历史、回执或部分事项版本。"""
    path = '/v1/shared-matters/' + shared_id
    bad = {'request_id': 'bad-grant', 'confirmed': True, 'expected_version': 1,
           'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer',
           'scope': 'packet', 'packet_title': '资料包', 'material_revisions': ['missing']}
    response = await shared_http['alice'].post(path + '/members', json=bad)
    assert response.status_code == 400, response.text
    matter = (await shared_http['alice'].get(path)).json()
    assert matter['version'] == 1
    assert (await shared_http['alice'].get(path + '/members')).json()['total'] == 1
    assert (await shared_http['alice'].get(path + '/events')).json()['total'] == 1
    retry = await shared_http['alice'].post(path + '/members', json={**bad, 'material_revisions': []})
    assert retry.status_code == 201, retry.text
    store = r3_world.platform.coordination
    with pytest.raises(ConflictError):
        async with store.transaction('acme', participants=('usr_alice',)) as tx:
            tx.shared.members.clear()
    assert (await shared_http['bob'].get(path)).status_code == 200


async def test_disabled_owner_blocks_changes_without_admin_takeover(shared_http, shared_id, r3_world):
    """离职负责人不自动改派；恢复本人身份后仍保留原权属，协作授权不复活。"""
    path = '/v1/shared-matters/' + shared_id
    response = await shared_http['alice'].post(path + '/members', json={
        'request_id': 'editor', 'confirmed': True, 'expected_version': 1,
        'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'editor'})
    assert response.status_code == 201, response.text
    identity = r3_world.platform.identity.store
    await identity.set_user_status('acme', 'usr_alice', 'disabled', reason='admin', at=100)
    blocked = await shared_http['bob'].patch(path, json={
        'request_id': 'disabled-owner-edit', 'confirmed': True, 'expected_version': 2,
        'content': '不能擅自推进'})
    assert blocked.status_code == 409, blocked.text
    assert (await shared_http['tenant_admin'].get(path)).status_code == 404
    await identity.set_user_status('acme', 'usr_alice', 'active', at=101)
    # 停用时原登录会话已撤销；用当前有效员工域调用核验权属，不伪造旧Cookie成功。
    from xuanpu_server.tenancy import Principal
    owner = Principal(role='tenant_member', tenant_id='acme', user_id='usr_alice', key_id=None)
    assert (await r3_world.platform.shared_matters.get(owner, shared_id))['owner_id'] == 'usr_alice'


async def test_confirmed_and_packet_constraints_are_server_enforced(shared_http, shared_id):
    """未确认、企图编辑packet、角色字段注入和空标题都由服务端拒绝。"""
    client = shared_http['alice']
    assert (await client.post('/v1/shared-matters', json={'request_id': 'unconfirmed', 'title': '事项'})).status_code == 400
    assert (await client.post('/v1/shared-matters', json={'request_id': 'blank', 'confirmed': True, 'title': ' '})).status_code == 400
    path = '/v1/shared-matters/' + shared_id
    body = {'request_id': 'invalid-packet', 'confirmed': True, 'expected_version': 1,
            'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'editor', 'scope': 'packet', 'packet_title': '范围'}
    assert (await client.post(path + '/members', json=body)).status_code == 400
    assert (await client.post(path + '/members', json={**body, 'role': 'viewer', 'tenant_id': 'beta'})).status_code == 400
    assert (await shared_http['bob'].get(path)).status_code == 404

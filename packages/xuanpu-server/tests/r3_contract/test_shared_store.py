"""共享事项的owner、版本回执与协调事务构成同一权属边界。"""
from __future__ import annotations


async def test_shared_cas_replay_and_explicit_owner_transfer(shared_http, shared_id):
    """唯一owner只能明确转交，不改身份角色或由管理员接管。"""
    http = shared_http
    path = '/v1/shared-matters/' + shared_id
    body = {'request_id': 'rename', 'confirmed': True, 'expected_version': 1, 'title': '改名后的事项'}
    first = await http['alice'].patch(path, json=body)
    assert first.status_code == 200, first.text
    assert (await http['alice'].patch(path, json=body)).json() == first.json()
    assert (await http['alice'].patch(path, json={**body, 'title': '不同参数'})).status_code == 409
    assert (await http['alice'].patch(path, json={**body, 'request_id': 'stale'})).status_code == 409
    transfer = await http['alice'].post(path + '/transfer-owner', json={
        'request_id': 'transfer', 'confirmed': True, 'expected_version': 2, 'user_id': 'usr_bob'})
    assert transfer.status_code == 200, transfer.text
    assert (await http['alice'].get(path)).status_code == 404
    owned = await http['bob'].get(path)
    assert owned.status_code == 200 and owned.json()['owner_id'] == 'usr_bob'
    assert (await http['bob'].get('/v1/me')).json()['role'] == 'tenant_member'
    members = await http['bob'].get(path + '/members')
    assert len([m for m in members.json()['items'] if m['role'] == 'owner' and m['status'] == 'active']) == 1


async def test_shared_feature_off_keeps_reads_and_revocation(shared_http, shared_id, r3_world):
    """关闭新共享保留已有读取与撤销，不回退私有reader。"""
    from xuanpu_server.shared_matters.models import SharedMatterConfig

    path = '/v1/shared-matters/' + shared_id
    member = await shared_http['alice'].post(path + '/members', json={
        'request_id': 'before-close', 'confirmed': True, 'expected_version': 1,
        'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer'})
    assert member.status_code == 201, member.text
    r3_world.platform.shared_matters.config = SharedMatterConfig()
    assert (await shared_http['bob'].get(path)).status_code == 200
    assert (await shared_http['alice'].post('/v1/shared-matters', json={
        'request_id': 'flag-off', 'confirmed': True, 'title': '不应创建'})).status_code == 403
    assert (await shared_http['alice'].post(path + '/members/' + member.json()['id'] + '/revoke', json={
        'request_id': 'after-close', 'confirmed': True, 'expected_version': 2})).status_code == 200
    assert (await shared_http['bob'].get(path)).status_code == 404


async def test_shared_list_counts_ignore_pagination_and_hidden_objects(shared_http, shared_id):
    """可见总数及分类不被当前页长度或他人私有事项污染。"""
    for index in range(3):
        created = await shared_http['bob'].post('/v1/shared-matters', json={
            'request_id': 'bob-' + str(index), 'confirmed': True, 'title': 'Bob事项' + str(index)})
        assert created.status_code == 201, created.text
    page = await shared_http['bob'].get('/v1/shared-matters?limit=1')
    assert page.status_code == 200, page.text
    assert page.json()['total'] == 3 and page.json()['counts']['active'] == 3
    assert len(page.json()['items']) == 1
    second = await shared_http['bob'].get('/v1/shared-matters', params={'limit': 1, 'cursor': page.json()['next_cursor']})
    assert second.json()['items'][0]['id'] != page.json()['items'][0]['id']
    assert (await shared_http['machine'].get('/v1/shared-matters')).status_code == 403

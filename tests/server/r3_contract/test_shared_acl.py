"""共享ACL在服务端过滤正文、列表、搜索和统计，私人工作台不受影响。"""
from __future__ import annotations


async def test_explicit_share_never_opens_private_workspace(shared_http, shared_id):
    """共享一个事项不等于共享本人工作台；管理员不能借管理身份读正文。"""
    http = shared_http
    private = await http['alice'].post('/v1/workspace/items', json={
        'request_id': 'private-create', 'kind': 'matter',
        'data': {'title': '私人事项', 'description': '私人正文'}})
    assert private.status_code == 200, private.text
    path = '/v1/shared-matters/' + shared_id
    assert (await http['bob'].get(path)).status_code == 404
    assert (await http['tenant_admin'].get(path)).status_code == 404
    shared = await http['alice'].post(path + '/members', json={
        'request_id': 'share-bob', 'confirmed': True, 'expected_version': 1,
        'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer'})
    assert shared.status_code == 201, shared.text
    assert (await http['bob'].get(path)).json()['content'] == '仅共享事项正文'
    assert (await http['bob'].get('/v1/workspace/items/' + private.json()['id'])).status_code == 404
    assert (await http['tenant_admin'].get(path)).status_code == 404
    listing = await http['tenant_admin'].get('/v1/shared-matters')
    assert listing.status_code == 200 and listing.json()['total'] == 0
    assert '共同核对材料' not in listing.text


async def test_packet_projection_cannot_search_or_count_other_content(shared_http, shared_id):
    """只给摘要的packet不泄露原事项名称、正文或成员数量。"""
    path = '/v1/shared-matters/' + shared_id
    response = await shared_http['alice'].post(path + '/members', json={
        'request_id': 'packet-bob', 'confirmed': True, 'expected_version': 1,
        'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer',
        'scope': 'packet', 'packet_title': '待核对的选定材料', 'include_content': False,
        'material_revisions': []})
    assert response.status_code == 201, response.text
    packet = await shared_http['bob'].get(path)
    assert packet.status_code == 200, packet.text
    assert packet.json()['title'] == '待核对的选定材料'
    assert all(secret not in packet.text for secret in ('共同核对材料', '仅共享事项正文', 'member_count', 'owner_id'))
    assert (await shared_http['bob'].get(path + '/members')).status_code == 404
    listing = await shared_http['bob'].get('/v1/shared-matters', params={'search': '仅共享事项正文'})
    assert listing.json()['total'] == 0
    listing = await shared_http['bob'].get('/v1/shared-matters', params={'search': '待核对'})
    assert listing.json()['total'] == 1


async def test_member_revocation_and_reenable_do_not_restore_grant(shared_http, shared_id, r3_world):
    """撤销和停用代次都在读取时检查，不等后台删除记录。"""
    path = '/v1/shared-matters/' + shared_id
    created = await shared_http['alice'].post(path + '/members', json={
        'request_id': 'grant-bob', 'confirmed': True, 'expected_version': 1,
        'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer'})
    assert created.status_code == 201, created.text
    member = created.json()
    revoke = await shared_http['alice'].post(path + '/members/' + member['id'] + '/revoke', json={
        'request_id': 'revoke-bob', 'confirmed': True, 'expected_version': 2})
    assert revoke.status_code == 200, revoke.text
    assert (await shared_http['bob'].get(path)).status_code == 404
    fresh = await shared_http['alice'].post(path + '/members', json={
        'request_id': 'grant-bob-again', 'confirmed': True, 'expected_version': 3,
        'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer'})
    assert fresh.status_code == 201, fresh.text
    identity = r3_world.platform.identity.store
    await identity.set_user_status('acme', 'usr_bob', 'disabled', reason='admin', at=100)
    await identity.set_user_status('acme', 'usr_bob', 'active', at=101)
    assert (await shared_http['bob'].get(path)).status_code == 404


async def test_unit_grant_requires_current_membership_and_selected_role(shared_http, shared_id, organization_ids, r3_world):
    """客户端传部门ID不构成权限；当前任职和职责缺一不可。"""
    admin, ids = shared_http['tenant_admin'], organization_ids
    member = await admin.post('/v1/admin/organization/memberships', json={
        'request_id': 'bob-member', 'user_id': 'usr_bob', 'unit_id': ids['a']})
    assert member.status_code == 201, member.text
    path = '/v1/shared-matters/' + shared_id
    grant = await shared_http['alice'].post(path + '/members', json={
        'request_id': 'unit-grant', 'confirmed': True, 'expected_version': 1,
        'subject_kind': 'unit', 'subject_id': ids['a'], 'role': 'viewer',
        'required_org_roles': ['unit_operator']})
    assert grant.status_code == 201, grant.text
    assert (await shared_http['bob'].get(path)).status_code == 404
    role = await admin.post('/v1/admin/organization/roles', json={
        'request_id': 'bob-operator', 'user_id': 'usr_bob', 'scope_id': ids['a'], 'role': 'unit_operator'})
    assert role.status_code == 201, role.text
    assert (await shared_http['bob'].get(path)).status_code == 200
    revoked = await admin.patch('/v1/admin/organization/roles/' + role.json()['id'], json={
        'request_id': 'revoke-bob-operator', 'expected_version': 1, 'status': 'revoked'})
    assert revoked.status_code == 200, revoked.text
    assert (await shared_http['bob'].get(path)).status_code == 404

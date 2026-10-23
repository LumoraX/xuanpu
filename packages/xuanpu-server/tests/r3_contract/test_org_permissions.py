"""组织管理权限按当前职责范围检查，URL前缀不决定权限。"""
from __future__ import annotations


async def test_member_cannot_grant_and_scoped_manager_cannot_escape(organization_http, organization_ids):
    """普通成员不能授职责，部门管理者不能操作其他部门或升为全企业管理。"""
    http, ids = organization_http, organization_ids
    body = {'request_id': 'grant', 'user_id': 'usr_alice', 'scope_id': ids['a'], 'role': 'org_manager'}
    response = await http['alice'].post('/v1/admin/organization/roles', json=body)
    assert response.status_code == 403
    response = await http['tenant_admin'].post('/v1/admin/organization/roles', json=body)
    assert response.status_code == 201, response.text
    allowed = await http['alice'].patch('/v1/admin/organization/units/' + ids['child'], json={
        'request_id': 'rename-child', 'expected_version': 1, 'name': '新一组'})
    assert allowed.status_code == 200, allowed.text
    outside = await http['alice'].patch('/v1/admin/organization/units/' + ids['b'], json={
        'request_id': 'rename-outside', 'expected_version': 1, 'name': '越界'})
    assert outside.status_code == 404, outside.text
    escalate = await http['alice'].post('/v1/admin/organization/roles', json={
        'request_id': 'escalate', 'user_id': 'usr_alice', 'scope_id': None, 'role': 'org_manager'})
    assert escalate.status_code == 403, escalate.text


async def test_api_cycle_cas_and_replay(organization_http, organization_ids):
    """循环拒绝不修改旧版本；重复写返回原回执，异参及过期版本冲突。"""
    admin, ids = organization_http['tenant_admin'], organization_ids
    root = '/v1/admin/organization/units/' + ids['a']
    response = await admin.patch(root, json={
        'request_id': 'cycle', 'expected_version': 1, 'parent_id': ids['child']})
    assert response.status_code == 409, response.text
    assert (await admin.get(root)).json()['parent_id'] is None
    body = {'request_id': 'rename', 'expected_version': 1, 'name': '研发'}
    first = await admin.patch(root, json=body)
    assert first.status_code == 200, first.text
    assert (await admin.patch(root, json=body)).json() == first.json()
    assert (await admin.patch(root, json={**body, 'name': '异参'})).status_code == 409
    assert (await admin.patch(root, json={**body, 'request_id': 'stale'})).status_code == 409


async def test_directory_minimal_counts_and_role_revocation(organization_http, organization_ids):
    """目录无需模型；统计独立分页，撤销职责立即拒绝新操作。"""
    http, ids = organization_http, organization_ids
    admin = http['tenant_admin']
    response = await admin.post('/v1/admin/organization/memberships', json={
        'request_id': 'member-a', 'user_id': 'usr_alice', 'unit_id': ids['a']})
    assert response.status_code == 201, response.text
    role = await admin.post('/v1/admin/organization/roles', json={
        'request_id': 'manager-a', 'user_id': 'usr_alice', 'scope_id': ids['a'], 'role': 'org_manager'})
    assert role.status_code == 201, role.text
    page = await admin.get('/v1/admin/organization/units?limit=1')
    assert page.status_code == 200, page.text
    assert page.json()['total'] == 3 and len(page.json()['items']) == 1
    second = await admin.get('/v1/admin/organization/units', params={'limit': 1, 'cursor': page.json()['next_cursor']})
    assert second.json()['items'][0]['id'] != page.json()['items'][0]['id']
    directory = await http['alice'].get('/v1/organization/directory')
    assert directory.status_code == 200, directory.text
    assert not any(k in directory.text for k in ('avatar_url', 'phone', 'email'))
    response = await admin.patch('/v1/admin/organization/roles/' + role.json()['id'], json={
        'request_id': 'revoke', 'expected_version': 1, 'status': 'revoked'})
    assert response.status_code == 200, response.text
    denied = await http['alice'].post('/v1/admin/organization/units', json={
        'request_id': 'after-revoke', 'name': '越权子组', 'parent_id': ids['a']})
    assert denied.status_code == 403, denied.text


async def test_organization_disabled_blocks_new_writes_but_keeps_reads(organization_http, organization_ids, r3_world):
    """关闭灰度保留目录及必要撤销；禁止新建、改名等新写入。"""
    from xuanpu_server.organization.config import OrganizationConfig

    r3_world.platform.organization.config = OrganizationConfig()
    admin = organization_http['tenant_admin']
    assert (await admin.get('/v1/admin/organization/units')).status_code == 200
    response = await admin.post('/v1/admin/organization/units', json={'request_id': 'disabled', 'name': '新部门'})
    assert response.status_code == 403


async def test_unauthorized_object_is_hidden(organization_http, organization_ids):
    """无组织职责员工不能区分真实对象和不存在对象。"""
    member = organization_http['bob']
    for identifier in (organization_ids['a'], 'missing'):
        assert (await member.get('/v1/admin/organization/units/' + identifier)).status_code == 404
        assert (await member.patch('/v1/admin/organization/units/' + identifier, json={
            'request_id': 'hidden-' + identifier, 'expected_version': 1, 'name': '越界'})).status_code == 404


async def test_feature_off_cannot_smuggle_edits_with_disable(organization_http, organization_ids, r3_world):
    """关闭新写后只允许必要撤销，不能借停用同时改写名称或组织关系。"""
    from xuanpu_server.organization.config import OrganizationConfig
    r3_world.platform.organization.config = OrganizationConfig()
    path = '/v1/admin/organization/units/' + organization_ids['b']
    response = await organization_http['tenant_admin'].patch(path, json={
        'request_id': 'disabled-rename', 'expected_version': 1, 'status': 'disabled', 'name': '夹带改名'})
    assert response.status_code == 403, response.text
    current = (await organization_http['tenant_admin'].get(path)).json()
    assert current['name'] == 'b' and current['version'] == 1
    assert (await organization_http['tenant_admin'].patch(path, json={
        'request_id': 'disable-only', 'expected_version': 1, 'status': 'disabled'})).status_code == 200

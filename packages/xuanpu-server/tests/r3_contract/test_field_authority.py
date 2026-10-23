"""字段来源切换须预览并确认，本地字段不会被下一次供应商登录覆盖。"""
from __future__ import annotations


async def test_authority_preview_apply_and_external_profile_refresh(organization_http, r3_world):
    """只允许显式字段，来源配置与实际身份刷新链路一致。"""
    admin = organization_http['tenant_admin']
    response = await admin.post('/v1/admin/organization/authority/previews', json={
        'request_id': 'local-name-preview', 'object_type': 'user', 'field': 'display_name', 'source': 'local'})
    assert response.status_code == 201, response.text
    preview = response.json()
    assert preview['status'] == 'pending' and preview['affected_count'] >= 3
    response = await admin.post('/v1/admin/organization/authority/previews/' + preview['id'] + '/apply', json={
        'request_id': 'local-name-apply', 'expected_version': 1})
    assert response.status_code == 200, response.text
    store = r3_world.platform.identity.store
    await store.update_user_profile('acme', 'usr_alice', display_name='本地姓名', avatar_url=None)
    # 飞书网页登录调用同一个身份仓储入口，并明确标注来源；企微登录不刷新本地姓名。
    await store.update_user_profile('acme', 'usr_alice', display_name='供应商姓名', avatar_url=None,
                                    last_login_at=123, source='feishu')
    user = await store.get_user('acme', 'usr_alice')
    assert user.display_name == '本地姓名' and user.last_login_at == 123
    role = await admin.post('/v1/admin/organization/authority/previews', json={
        'request_id': 'supplier-admin', 'object_type': 'user', 'field': 'role', 'source': 'local'})
    assert role.status_code == 400


async def test_authority_requires_current_preview_and_explicit_admin(organization_http):
    """旧预览不能覆盖更新后的来源版本；普通员工不能配置来源。"""
    http = organization_http
    body = {'request_id': 'p1', 'object_type': 'unit', 'field': 'name', 'source': 'local'}
    assert (await http['alice'].post('/v1/admin/organization/authority/previews', json=body)).status_code == 403
    first = await http['tenant_admin'].post('/v1/admin/organization/authority/previews', json=body)
    assert first.status_code == 201, first.text
    second = await http['tenant_admin'].post('/v1/admin/organization/authority/previews', json={**body, 'request_id': 'p2'})
    assert second.status_code == 201, second.text
    path = '/v1/admin/organization/authority/previews/'
    apply_body = {'request_id': 'apply-p1', 'expected_version': 1}
    result = await http['tenant_admin'].post(path + first.json()['id'] + '/apply', json=apply_body)
    assert result.status_code == 200, result.text
    assert (await http['tenant_admin'].post(path + first.json()['id'] + '/apply', json=apply_body)).json() == result.json()
    stale = await http['tenant_admin'].post(path + second.json()['id'] + '/apply', json={
        'request_id': 'apply-stale', 'expected_version': 1})
    assert stale.status_code == 409, stale.text


async def test_authority_installation_changed_after_preview_is_rejected(organization_http, organization_source):
    """安装在确认前停用，旧来源预览不能切换到已经失效的安装。"""
    admin = organization_http['tenant_admin']
    cid, _ = organization_source
    preview = await admin.post('/v1/admin/organization/authority/previews', json={
        'request_id': 'installation-preview', 'source': cid, 'object_type': 'unit', 'field': 'name'})
    assert preview.status_code == 201, preview.text
    disabled = await admin.patch('/v1/admin/connections/' + cid, json={
        'request_id': 'disable-source', 'expected_version': 2, 'status': 'disabled'})
    assert disabled.status_code == 200, disabled.text
    response = await admin.post('/v1/admin/organization/authority/previews/' + preview.json()['id'] + '/apply',
                               json={'request_id': 'apply-disabled-source', 'expected_version': 1})
    assert response.status_code == 409, response.text

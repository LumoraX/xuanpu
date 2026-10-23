"""外部目录必须先预览，只有明确选中的观察结果可以进入玄圃。"""
from __future__ import annotations


async def _authority(admin, source, object_type, field):
    """经原生确认接口设置测试字段来源。"""
    preview = await admin.post('/v1/admin/organization/authority/previews', json={
        'request_id': 'authority-' + object_type + field, 'source': source,
        'object_type': object_type, 'field': field})
    assert preview.status_code == 201, preview.text
    result = await admin.post('/v1/admin/organization/authority/previews/' + preview.json()['id'] + '/apply',
                              json={'request_id': 'confirm-' + object_type + field, 'expected_version': 1})
    assert result.status_code == 200, result.text


async def test_directory_preview_explicit_apply_and_mapping(organization_http, organization_source):
    """预览不写组织；分批显式应用保留来源映射，重试不重复创建。"""
    admin = organization_http['tenant_admin']
    cid, state = organization_source
    state['extra_user'] = True
    for object_type, field in [('unit', 'name'), ('unit', 'parent_id'), ('membership', 'membership')]:
        await _authority(admin, cid, object_type, field)
    response = await admin.post('/v1/admin/organization/import-previews', json={
        'request_id': 'capture', 'connection_id': cid})
    assert response.status_code == 201, response.text
    preview = response.json()
    assert preview['complete'] is False and preview['scope'] == 'application_visible'
    assert preview['capture_started_at'] <= preview['capture_finished_at']
    assert len(preview['pages']) == 3
    assert any(c['code'] == 'unmapped_user' for c in preview['conflicts'])
    assert (await admin.get('/v1/admin/organization/units')).json()['total'] == 0
    changes = [c for c in preview['changes'] if c['object_type'] == 'unit']
    assert len(changes) == 2
    body = {'request_id': 'apply', 'expected_version': preview['version'],
            'source_revision': preview['source_revision'], 'selected_changes': [
                {'change_id': c['id'], 'action': c['action']} for c in changes]}
    path = '/v1/admin/organization/import-previews/' + preview['id'] + '/apply'
    first = await admin.post(path, json=body)
    assert first.status_code == 200, first.text
    assert (await admin.post(path, json=body)).json() == first.json()
    assert (await admin.get('/v1/admin/organization/units')).json()['total'] == 2
    assert (await organization_http['alice'].post(path, json=body)).status_code == 403


async def test_partial_directory_missing_users_never_disable(organization_http, organization_source):
    """某页失败可预览已观察的范围，但不能由缺失推导离职或撤权。"""
    admin = organization_http['tenant_admin']
    cid, state = organization_source
    state['fail_page'] = True
    response = await admin.post('/v1/admin/organization/import-previews', json={
        'request_id': 'partial', 'connection_id': cid})
    assert response.status_code == 201, response.text
    preview = response.json()
    assert preview['complete'] is False and preview['errors']
    assert all(c['action'] not in ('disable', 'delete', 'revoke') for c in preview['changes'])
    assert (await admin.get('/v1/users/usr_alice')).json()['status'] == 'active'


async def test_source_changes_after_preview_rejects_apply(organization_http, organization_source):
    """应用前重新采集，供应商摘要变化拒绝整个选中批次。"""
    admin = organization_http['tenant_admin']
    cid, state = organization_source
    await _authority(admin, cid, 'unit', 'name')
    await _authority(admin, cid, 'unit', 'parent_id')
    response = await admin.post('/v1/admin/organization/import-previews', json={
        'request_id': 'before-source-change', 'connection_id': cid})
    assert response.status_code == 201, response.text
    preview = response.json()
    assert preview['changes']
    state['name'] = '供应商已改名'
    response = await admin.post('/v1/admin/organization/import-previews/' + preview['id'] + '/apply', json={
        'request_id': 'apply-stale-source', 'expected_version': preview['version'],
        'source_revision': preview['source_revision'], 'selected_changes': [
            {'change_id': c['id'], 'action': c['action']} for c in preview['changes'] if c['object_type'] == 'unit']})
    assert response.status_code == 409, response.text
    assert (await admin.get('/v1/admin/organization/units')).json()['total'] == 0


async def test_observed_user_name_uses_same_transaction_and_local_cas(organization_http, organization_source, r3_world):
    """明确的姓名权威允许导入；预览后本地变更会阻止覆盖。"""
    admin = organization_http['tenant_admin']
    cid, state = organization_source
    await _authority(admin, cid, 'user', 'display_name')
    state['employee_name'] = '供应商姓名'
    preview_response = await admin.post('/v1/admin/organization/import-previews', json={
        'request_id': 'name-preview', 'connection_id': cid})
    assert preview_response.status_code == 201, preview_response.text
    preview = preview_response.json()
    changes = [c for c in preview['changes'] if c['object_type'] == 'user']
    assert len(changes) == 1, '缺少已绑定员工的姓名差异'
    body = {'request_id': 'name-apply', 'expected_version': preview['version'],
            'source_revision': preview['source_revision'],
            'selected_changes': [{'change_id': c['id'], 'action': c['action']} for c in changes]}
    response = await admin.post('/v1/admin/organization/import-previews/' + preview['id'] + '/apply', json=body)
    assert response.status_code == 200, response.text
    assert (await r3_world.platform.identity.store.get_user('acme', 'usr_alice')).display_name == '供应商姓名'
    assert (await admin.post('/v1/admin/organization/import-previews/' + preview['id'] + '/apply', json=body)).json() == response.json()

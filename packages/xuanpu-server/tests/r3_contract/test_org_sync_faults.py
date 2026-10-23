"""目录选中批次的依赖冲突和跨域回滚，不靠供应商空页猜测业务状态。"""
from __future__ import annotations


async def _authority(admin, cid, object_type, field):
    """经原生接口确认当前测试字段归属。"""
    response = await admin.post('/v1/admin/organization/authority/previews', json={
        'request_id': object_type + field, 'source': cid, 'object_type': object_type, 'field': field})
    assert response.status_code == 201, response.text
    response = await admin.post('/v1/admin/organization/authority/previews/' + response.json()['id'] + '/apply',
                               json={'request_id': 'apply-' + object_type + field, 'expected_version': 1})
    assert response.status_code == 200, response.text


async def test_selecting_child_without_unapplied_parent_conflicts(organization_http, organization_source):
    """不暗中补选父部门；缺少前置选项时整批409且没有部分记录。"""
    admin = organization_http['tenant_admin']
    cid, _ = organization_source
    for field in ('name', 'parent_id'):
        await _authority(admin, cid, 'unit', field)
    response = await admin.post('/v1/admin/organization/import-previews', json={'request_id': 'preview-child', 'connection_id': cid})
    assert response.status_code == 201, response.text
    preview = response.json()
    child = next(c for c in preview['changes'] if c['object_type'] == 'unit' and c['data']['parent_id'] is not None)
    response = await admin.post('/v1/admin/organization/import-previews/' + preview['id'] + '/apply', json={
        'request_id': 'child-only', 'expected_version': 1, 'source_revision': preview['source_revision'],
        'selected_changes': [{'change_id': child['id'], 'action': 'create'}]})
    assert response.status_code == 409, response.text
    assert (await admin.get('/v1/admin/organization/units')).json()['total'] == 0


async def test_profile_conflict_rolls_back_unit_mapping_receipt_and_event(organization_http, organization_source, r3_world):
    """姓名CAS失败发生在同一提交边界，不能留下已导入部门或来源映射。"""
    admin = organization_http['tenant_admin']
    cid, state = organization_source
    for object_type, field in [('unit', 'name'), ('unit', 'parent_id'), ('user', 'display_name')]:
        await _authority(admin, cid, object_type, field)
    state['employee_name'] = '来源准备的新姓名'
    response = await admin.post('/v1/admin/organization/import-previews', json={
        'request_id': 'preview-atomic', 'connection_id': cid})
    assert response.status_code == 201, response.text
    preview = response.json()
    await r3_world.platform.identity.store.update_user_profile('acme', 'usr_alice',
        display_name='预览后的另一次修改', avatar_url=None, source=cid)
    selected = [c for c in preview['changes'] if c['object_type'] in ('unit', 'user')]
    response = await admin.post('/v1/admin/organization/import-previews/' + preview['id'] + '/apply', json={
        'request_id': 'atomic-apply', 'expected_version': 1, 'source_revision': preview['source_revision'],
        'selected_changes': [{'change_id': c['id'], 'action': c['action']} for c in selected]})
    assert response.status_code == 409, response.text
    assert (await admin.get('/v1/admin/organization/units')).json()['total'] == 0
    async with r3_world.platform.coordination.transaction('acme') as tx:
        assert not tx.organization.mappings
        assert tx.organization.previews[preview['id']].version == 1
        assert not [e for e in tx.state.events.values() if e.kind == 'directory_batch_applied']

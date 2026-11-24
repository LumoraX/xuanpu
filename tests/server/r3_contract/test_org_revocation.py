"""停用、重新启用和后台收尾之间不能出现旧职责恢复窗口。"""
from __future__ import annotations


async def _grant(http, unit):
    """由真实原生权限接口授予测试管理职责。"""
    response = await http['tenant_admin'].post('/v1/admin/organization/roles', json={
        'request_id': 'revoke-manager', 'user_id': 'usr_alice', 'scope_id': unit, 'role': 'org_manager'})
    assert response.status_code == 201, response.text
    return response.json()


async def test_disable_then_enable_never_restores_old_role(organization_http, organization_ids, r3_world):
    """模拟身份事务提交后进程崩溃，收尾尚未执行；重新启用也不能恢复旧职责。"""
    await _grant(organization_http, organization_ids['a'])
    identity = r3_world.platform.identity.store
    await identity.set_user_status('acme', 'usr_alice', 'disabled', reason='admin', at=100)
    await identity.set_user_status('acme', 'usr_alice', 'active', at=101)
    result = await organization_http['alice'].post('/v1/admin/organization/units', json={
        'request_id': 'old-role-after-enable', 'name': '无效职责', 'parent_id': organization_ids['a']})
    assert result.status_code == 403, result.text
    assert (await organization_http['alice'].get('/v1/organization/me')).json()['roles'] == []


async def test_disabled_employee_role_can_be_explicitly_revoked(organization_http, organization_ids, r3_world):
    """管理员收回已停用员工的职责，不要求先把员工重新启用。"""
    role = await _grant(organization_http, organization_ids['a'])
    await r3_world.platform.identity.store.set_user_status('acme', 'usr_alice', 'disabled', reason='admin', at=100)
    result = await organization_http['tenant_admin'].patch('/v1/admin/organization/roles/' + role['id'], json={
        'request_id': 'revoke-disabled', 'expected_version': 1, 'status': 'revoked'})
    assert result.status_code == 200, result.text
    assert result.json()['epoch'] == 2


async def test_disabled_event_survives_without_in_process_listener(organization_http, organization_ids, r3_world):
    """停用事务留下稳定事件，重启扫描逐条收尾且重复处理不改版本。"""
    role = await _grant(organization_http, organization_ids['a'])
    platform = r3_world.platform
    await platform.identity.store.set_user_status('acme', 'usr_alice', 'disabled', reason='admin', at=100)
    async with platform.coordination.transaction('acme') as tx:
        pending = [e for e in tx.pending_events('organization') if e.kind == 'user_disabled']
        assert len(pending) == 1
    assert hasattr(platform, 'organization_lifecycle'), '缺少持久治理事件恢复入口'
    await platform.organization_lifecycle.recover('acme')
    await platform.organization_lifecycle.recover('acme')
    async with platform.coordination.transaction('acme') as tx:
        assert tx.organization.roles[role['id']].status == 'revoked'
        assert tx.organization.roles[role['id']].version == 2
        assert not [e for e in tx.pending_events('organization') if e.kind == 'user_disabled']


async def test_explicit_new_generation_role_survives_old_disable_event(organization_http, organization_ids, r3_world):
    """重新启用后明确授予新职责，不必等待旧收尾，也不被迟到事件覆盖。"""
    old = await _grant(organization_http, organization_ids['a'])
    platform = r3_world.platform
    await platform.identity.store.set_user_status('acme', 'usr_alice', 'disabled', reason='admin', at=100)
    await platform.identity.store.set_user_status('acme', 'usr_alice', 'active', at=101)
    response = await organization_http['tenant_admin'].post('/v1/admin/organization/roles', json={
        'request_id': 'explicit-new-generation', 'user_id': 'usr_alice',
        'scope_id': organization_ids['a'], 'role': 'org_manager'})
    assert response.status_code == 201, response.text
    new = response.json()
    await platform.organization_lifecycle.recover('acme')
    async with platform.coordination.transaction('acme') as tx:
        assert tx.organization.roles[old['id']].status == 'revoked'
        assert tx.organization.roles[new['id']].status == 'active'
        assert tx.organization.roles[new['id']].user_epoch == 3

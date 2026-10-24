"""选择投影和组织授权身份均冻结，重授不会复活未检查的旧上下文。"""
from __future__ import annotations

import pytest

from .test_shared_context import context_for
from .test_shared_notifications import drain


async def member(admin, unit, request):
    """从正式组织入口安排测试员工。"""
    result = await admin.post('/v1/admin/organization/memberships', json={
        'request_id': request, 'user_id': 'usr_bob', 'unit_id': unit})
    assert result.status_code == 201, result.text
    return result.json()


async def grant(client, shared_id, request, version, kind, target, **extra):
    """来源权限由拥有者明确确认，不借管理员越权。"""
    result = await client.post('/v1/shared-matters/' + shared_id + '/members', json={
        'request_id': request, 'confirmed': True, 'expected_version': version,
        'subject_kind': kind, 'subject_id': target, 'role': 'viewer', **extra})
    assert result.status_code == 201, result.text
    return result.json()


async def test_all_packet_bodies_match_confirmed_projection(shared_http, shared_id, organization_ids):
    """直接资料包和任职资料包聚合后的正文均属于明确选择，旧选择号不能确认新组合。"""
    alice, bob, admin = (shared_http[k] for k in ('alice', 'bob', 'tenant_admin'))
    await member(admin, organization_ids['a'], 'multi-packet-member')
    path = '/v1/shared-matters/' + shared_id
    await grant(alice, shared_id, 'multi-packet-direct', 1, 'user', 'usr_bob', scope='packet',
        packet_title='第一份资料', include_content=True)
    before = (await bob.get(path)).json()
    changed = await alice.patch(path, json={'request_id': 'multi-packet-change', 'confirmed': True,
        'expected_version': 2, 'content': '另一份明确共享正文'})
    assert changed.status_code == 200, changed.text
    await grant(alice, shared_id, 'multi-packet-unit', 3, 'unit', organization_ids['a'],
        scope='packet', packet_title='第二份资料', include_content=True)
    preview = (await bob.get(path)).json()
    assert (await context_for(bob, shared_id, 'stale-multi-packet', before['version'])).status_code == 409
    created = await context_for(bob, shared_id, 'current-multi-packet', preview['version'])
    assert created.status_code == 201, created.text
    read = (await bob.get('/v1/shared-contexts/' + created.json()['id'] + '/read')).json()
    assert read['content'] == preview['content'] == '仅共享事项正文\n另一份明确共享正文'


async def test_replacement_packet_rejects_old_confirmation(shared_http, shared_id):
    """新资料包版本重新从一开始，选择号仍必须绑定授权身份。"""
    alice, bob = shared_http['alice'], shared_http['bob']
    path = '/v1/shared-matters/' + shared_id
    old = await grant(alice, shared_id, 'replace-old', 1, 'user', 'usr_bob', scope='packet',
        packet_title='旧资料', include_content=True)
    preview = (await bob.get(path)).json()
    revoked = await alice.post(path + '/members/' + old['id'] + '/revoke', json={
        'request_id': 'replace-revoke', 'confirmed': True, 'expected_version': 2})
    assert revoked.status_code == 200
    await grant(alice, shared_id, 'replace-new', 3, 'user', 'usr_bob', scope='packet',
        packet_title='新资料', include_content=True)
    assert (await context_for(bob, shared_id, 'replace-stale-choice', preview['version'])).status_code == 409


@pytest.mark.parametrize('authority', ['memberships', 'roles'])
async def test_regranted_organization_authority_cannot_revive_context(shared_http, shared_id, organization_ids, authority):
    """撤权期间不检查旧上下文，重新任职或重新授职仍不能复用原上下文。"""
    alice, bob, admin = (shared_http[k] for k in ('alice', 'bob', 'tenant_admin'))
    assignment = await member(admin, organization_ids['a'], 'authority-member')
    extra = {}
    if authority == 'roles':
        body = {'request_id': 'authority-role', 'user_id': 'usr_bob', 'scope_id': organization_ids['a'], 'role': 'unit_operator'}
        result = await admin.post('/v1/admin/organization/roles', json=body)
        assert result.status_code == 201, result.text
        assignment = result.json()
        extra = {'required_org_roles': ['unit_operator']}
    await grant(alice, shared_id, 'authority-unit', 1, 'unit', organization_ids['a'], **extra)
    created = await context_for(bob, shared_id, 'authority-source', 2)
    assert created.status_code == 201, created.text
    result = await admin.patch('/v1/admin/organization/' + authority + '/' + assignment['id'], json={
        'request_id': 'authority-revoke', 'expected_version': 1, 'status': 'revoked'})
    assert result.status_code == 200, result.text
    assert (await bob.get('/v1/shared-matters/' + shared_id)).status_code == 404
    if authority == 'memberships':
        await member(admin, organization_ids['a'], 'authority-new-member')
    else:
        result = await admin.post('/v1/admin/organization/roles', json={**body, 'request_id': 'authority-new-role'})
        assert result.status_code == 201, result.text
    assert (await bob.get('/v1/shared-matters/' + shared_id)).status_code == 200
    read = await bob.get('/v1/shared-contexts/' + created.json()['id'] + '/read')
    assert read.status_code == 403, read.text


async def test_inbox_visibility_revision_includes_current_authority(shared_http, shared_id, organization_ids, r3_world):
    """仍可读限定资料包时，失去整事项任职也必须推送权限变化提示。"""
    alice, bob, admin = (shared_http[k] for k in ('alice', 'bob', 'tenant_admin'))
    assignment = await member(admin, organization_ids['a'], 'hint-member')
    await grant(alice, shared_id, 'hint-packet', 1, 'user', 'usr_bob', scope='packet', packet_title='限定资料')
    await grant(alice, shared_id, 'hint-unit', 2, 'unit', organization_ids['a'])
    await drain(r3_world.platform)
    before = (await bob.get('/v1/shared-notifications')).json()
    result = await admin.patch('/v1/admin/organization/memberships/' + assignment['id'], json={
        'request_id': 'hint-revoke', 'expected_version': 1, 'status': 'revoked'})
    assert result.status_code == 200, result.text
    after = (await bob.get('/v1/shared-notifications')).json()
    assert before['total'] == after['total']
    assert before['visibility_revision'] != after['visibility_revision']


async def test_inbox_authority_hint_without_existing_notification(shared_http, shared_id, organization_ids, r3_world):
    """先授权部门后任职的员工没有旧通知，撤权仍改变权限摘要。"""
    alice, bob, admin = (shared_http[k] for k in ('alice', 'bob', 'tenant_admin'))
    await grant(alice, shared_id, 'empty-hint-unit', 1, 'unit', organization_ids['a'])
    await drain(r3_world.platform)
    assignment = await member(admin, organization_ids['a'], 'empty-hint-member')
    assert (await bob.get('/v1/shared-matters/' + shared_id)).status_code == 200
    before = (await bob.get('/v1/shared-notifications')).json()
    result = await admin.patch('/v1/admin/organization/memberships/' + assignment['id'], json={
        'request_id': 'empty-hint-revoke', 'expected_version': 1, 'status': 'revoked'})
    assert result.status_code == 200, result.text
    assert (await bob.get('/v1/shared-matters/' + shared_id)).status_code == 404
    after = (await bob.get('/v1/shared-notifications')).json()
    assert before['total'] == after['total'] == 0
    assert before['permission_revision'] != after['permission_revision']
    assert before['visibility_revision'] != after['visibility_revision']

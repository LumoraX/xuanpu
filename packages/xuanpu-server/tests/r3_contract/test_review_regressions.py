"""独立复核发现的职责复活、迟到对象和撤回配额问题。"""
from __future__ import annotations

import asyncio
import time

import pytest


async def test_revoked_manager_cannot_be_resurrected_by_scoped_manager(organization_http, organization_ids):
    """撤销是旧职责的终态，重新授予必须走同一授权限制的新建入口。"""
    http, unit = organization_http, organization_ids['b']
    granted = {}
    for user in ('alice', 'bob'):
        response = await http['tenant_admin'].post('/v1/admin/organization/roles', json={
            'request_id': 'manager-' + user, 'user_id': 'usr_' + user, 'scope_id': unit, 'role': 'org_manager'})
        assert response.status_code == 201, response.text
        granted[user] = response.json()['id']
    response = await http['tenant_admin'].patch('/v1/admin/organization/roles/' + granted['bob'], json={
        'request_id': 'revoke-bob', 'expected_version': 1, 'status': 'revoked'})
    assert response.status_code == 200
    denied = await http['alice'].post('/v1/admin/organization/roles', json={
        'request_id': 'new-bob-manager', 'user_id': 'usr_bob', 'scope_id': unit, 'role': 'org_manager'})
    assert denied.status_code == 403
    resurrected = await http['alice'].patch('/v1/admin/organization/roles/' + granted['bob'], json={
        'request_id': 'resurrect-bob', 'expected_version': 2, 'status': 'active'})
    assert resurrected.status_code == 409, resurrected.text
    denied = await http['bob'].post('/v1/admin/organization/units', json={
        'request_id': 'not-a-manager', 'name': '不可新建', 'parent_id': unit})
    assert denied.status_code == 403


async def test_late_copy_after_cleanup_is_collected_again(shared_http, shared_id, r3_world, monkeypatch):
    """一次删除并不证明过期writer不会晚到；持久墓碑需再次回收。"""
    from xuanpu_server.shared_matters.object_recovery import PublicationRecovery
    path = '/v1/shared-matters/' + shared_id
    materials, objects = r3_world.platform.shared_materials, r3_world.platform.objects
    put = objects.put
    started, release, stored = asyncio.Event(), asyncio.Event(), asyncio.Event()
    keys = []
    async def late_put(key, data, **kwargs):
        if '/shared/' in key:
            keys.append(key);started.set();await release.wait()
        await put(key, data, **kwargs)
        if '/shared/' in key:
            stored.set();await asyncio.Future()
    monkeypatch.setattr(objects, 'put', late_put)
    task = asyncio.create_task(shared_http['alice'].post(path + '/material-uploads', params={
        'name': '迟到.txt', 'request_id': 'late-copy', 'confirmed': 'true', 'expected_version': 1}, content=b'late'))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        now = time.time() + 31
        monkeypatch.setattr(materials.service, 'now', lambda: now)
        async with materials.service.store.transaction('acme') as tx:
            publication = next(iter(tx.shared.publications.values()))
        withdrawn = await shared_http['alice'].post(path + '/materials/' + publication.material_id + '/withdraw', json={
            'request_id': 'withdraw-before-put', 'confirmed': True, 'expected_version': 2})
        assert withdrawn.status_code == 200, withdrawn.text
        await PublicationRecovery(materials).recover('acme')
        release.set();await asyncio.wait_for(stored.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await objects.exists(keys[0])
        await PublicationRecovery(materials).recover('acme')
        assert not await objects.exists(keys[0])
    finally:
        task.cancel();await asyncio.gather(task, return_exceptions=True)


async def test_withdrawn_uncleaned_copy_keeps_space_reserved(shared_http, shared_id, r3_world, monkeypatch):
    """待发布副本撤回不立即腾空磁盘预算。"""
    from xuanpu_server.shared_matters.models import SharedMatterConfig
    materials, objects = r3_world.platform.shared_materials, r3_world.platform.objects
    materials.service.config = SharedMatterConfig(enabled=True, tenants=('acme',), max_storage_bytes=4)
    path = '/v1/shared-matters/' + shared_id
    put, stored = objects.put, asyncio.Event()
    async def pause(key, data, **kwargs):
        await put(key, data, **kwargs)
        if '/shared/' in key:
            stored.set();await asyncio.Future()
    monkeypatch.setattr(objects, 'put', pause)
    task = asyncio.create_task(shared_http['alice'].post(path + '/material-uploads', params={
        'name': '保留.txt', 'request_id': 'reserve', 'confirmed': 'true', 'expected_version': 1}, content=b'1234'))
    await asyncio.wait_for(stored.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with materials.service.store.transaction('acme') as tx:
        publication = next(iter(tx.shared.publications.values()))
    withdrawn = await shared_http['alice'].post(path + '/materials/' + publication.material_id + '/withdraw', json={
        'request_id': 'withdraw-pending', 'confirmed': True, 'expected_version': 2})
    assert withdrawn.status_code == 200
    monkeypatch.setattr(objects, 'put', put)
    again = await shared_http['alice'].post(path + '/material-uploads', params={
        'name': '第二个.txt', 'request_id': 'second-reserve', 'confirmed': 'true', 'expected_version': 3}, content=b'1234')
    assert again.status_code == 409, again.text
    # 即使租期结束后删除成功，当前进程的未知writer也不能被当成确定腾空。
    from xuanpu_server.shared_matters.object_recovery import PublicationRecovery
    now = time.time() + 31
    monkeypatch.setattr(materials.service, 'now', lambda: now)
    await PublicationRecovery(materials).recover('acme')
    still_reserved = await shared_http['alice'].post(path + '/material-uploads', params={
        'name': '第三个.txt', 'request_id': 'after-gc', 'confirmed': 'true', 'expected_version': 3}, content=b'1234')
    assert still_reserved.status_code == 409, still_reserved.text


async def test_new_generation_does_not_prove_old_writer_stopped(shared_http, shared_id, r3_world, monkeypatch):
    """旧HTTP线程仍可在新guard取得后写盘，代际变化不能用作释放预留的凭据。"""
    from xuanpu_server.errors import ConflictError
    from xuanpu_server.shared_matters import publication as module
    from xuanpu_server.shared_matters.models import SharedMatterConfig
    from xuanpu_server.shared_matters.object_recovery import PublicationRecovery
    from xuanpu_server.tenancy import Principal
    materials, objects = r3_world.platform.shared_materials, r3_world.platform.objects
    materials.service.config = SharedMatterConfig(enabled=True, tenants=('acme',), max_storage_bytes=4)
    path = '/v1/shared-matters/' + shared_id
    started = asyncio.Event()
    async def still_writing(key, data, **kwargs):
        started.set();await asyncio.Future()
    monkeypatch.setattr(objects, 'put', still_writing)
    task = asyncio.create_task(shared_http['alice'].post(path + '/material-uploads', params={
        'name': '旧writer.txt', 'request_id': 'old-writer', 'confirmed': 'true', 'expected_version': 1}, content=b'1234'))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        async with materials.service.store.transaction('acme') as tx:
            intent = next(iter(tx.shared.publications.values()))
        response = await shared_http['alice'].post(path + '/materials/' + intent.material_id + '/withdraw', json={
            'request_id': 'withdraw-old', 'confirmed': True, 'expected_version': 2})
        assert response.status_code == 200
        now = time.time() + 31
        monkeypatch.setattr(materials.service, 'now', lambda: now)
        monkeypatch.setattr(module, 'COPY_PROCESS_GENERATION', 'simulated-new-generation')
        await PublicationRecovery(materials).recover('acme')
        actor = Principal(role='tenant_member', tenant_id='acme', user_id='usr_alice', key_id=None)
        with pytest.raises(ConflictError):
            await materials.publications.prepare(actor, shared_id, request_id='new-reservation',
                expected=3, data=b'1234', name='第二个.txt')
    finally:
        task.cancel();await asyncio.gather(task, return_exceptions=True)


async def test_platform_close_cancels_inflight_publication(shared_http, shared_id, r3_world, monkeypatch):
    """实例丢失持有权时同样走close，旧HTTP发布不能随后标记ready。"""
    platform = r3_world.platform
    started = asyncio.Event()
    async def blocked_put(key, data, **kwargs):
        started.set();await asyncio.Future()
    monkeypatch.setattr(platform.objects, 'put', blocked_put)
    task = asyncio.create_task(shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/material-uploads',
        params={'name': '关闭前.txt', 'request_id': 'close-during-copy', 'confirmed': 'true', 'expected_version': 1}, content=b'private'))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        await asyncio.wait_for(platform.close(), timeout=10)
        assert task.cancelled()
        async with platform.coordination.transaction('acme') as tx:
            assert all(p.status == 'pending' and not p.copy_closed for p in tx.shared.publications.values())
            assert not tx.shared.revisions
    finally:
        task.cancel();await asyncio.gather(task, return_exceptions=True)

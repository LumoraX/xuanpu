"""真实双后端和对象仓储上的发布竞争、取消窗口与校验失败。"""
from __future__ import annotations

import asyncio
import time

import pytest


async def test_revoked_publisher_cannot_finish_copied_material(shared_http, shared_id, r3_world, monkeypatch):
    """复制后撤销编辑者，最终提交不得公开副本，失败对象必须清理。"""
    alice, bob = shared_http['alice'], shared_http['bob']
    path = '/v1/shared-matters/' + shared_id
    member = (await alice.post(path + '/members', json={'request_id': 'editor', 'confirmed': True,
        'expected_version': 1, 'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'editor'})).json()
    objects = r3_world.platform.objects
    put = objects.put
    copied = []
    async def revoke_after_put(key, data, **kwargs):
        await put(key, data, **kwargs)
        if '/shared/' in key:
            copied.append(key)
            revoke = await alice.post(path + '/members/' + member['id'] + '/revoke', json={
                'request_id': 'revoke-during-copy', 'confirmed': True, 'expected_version': 3})
            assert revoke.status_code == 200, revoke.text
    monkeypatch.setattr(objects, 'put', revoke_after_put)
    response = await bob.post(path + '/material-uploads', params={'name': '成果.txt',
        'request_id': 'publish-revoked', 'confirmed': 'true', 'expected_version': 2}, content=b'copy-never-public')
    assert response.status_code == 202, response.text
    result = (await alice.get(path + '/publications/' + response.json()['id'])).json()
    assert result['status'] == 'failed'
    assert (await alice.get(path + '/materials')).json()['total'] == 0
    assert (await bob.get(path)).status_code == 404
    assert copied
    for key in copied:
        assert not await objects.exists(key)


async def test_canceled_copy_is_recovered_from_durable_object(shared_http, shared_id, r3_world, monkeypatch):
    """请求取消后以新恢复器读取持久意图；这里只证明取消窗口，SIGKILL另验。"""
    from xuanpu_server.shared_matters.object_recovery import PublicationRecovery
    path = '/v1/shared-matters/' + shared_id
    materials = r3_world.platform.shared_materials
    objects = r3_world.platform.objects
    put = objects.put
    stored = asyncio.Event()
    async def pause_after_put(key, data, **kwargs):
        await put(key, data, **kwargs)
        if '/shared/' in key:
            stored.set()
            await asyncio.Future()
    monkeypatch.setattr(objects, 'put', pause_after_put)
    task = asyncio.create_task(shared_http['alice'].post(path + '/material-uploads',
        params={'name': '恢复.txt', 'request_id': 'cancel-after-copy', 'confirmed': 'true', 'expected_version': 1}, content=b'recover-me'))
    await asyncio.wait_for(stored.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(objects, 'put', put)
    async with materials.service.store.transaction('acme') as tx:
        publication = next(p for p in tx.shared.publications.values() if p.matter_id == shared_id)
        assert publication.status == 'pending'
    now = time.time() + 31
    monkeypatch.setattr(materials.service, 'now', lambda: now)
    assert await PublicationRecovery(materials).recover('acme') == 1
    result = (await shared_http['alice'].get(path + '/publications/' + publication.id)).json()
    assert result['status'] == 'ready'
    download = await shared_http['alice'].get(path + '/materials/' + publication.material_id + '/content', params={'revision': 1})
    assert download.content == b'recover-me'
    assert await PublicationRecovery(materials).recover('acme') == 0


async def test_corrupt_object_never_becomes_ready(shared_http, shared_id, r3_world, monkeypatch):
    """元数据SHA不能代替真实对象校验；损坏副本不开放下载。"""
    path = '/v1/shared-matters/' + shared_id
    objects = r3_world.platform.objects
    put = objects.put
    async def corrupt(key, data, **kwargs):
        await put(key, b'corrupt' if '/shared/' in key else data, **kwargs)
    monkeypatch.setattr(objects, 'put', corrupt)
    response = await shared_http['alice'].post(path + '/material-uploads', params={
        'name': '校验.txt', 'request_id': 'corrupt', 'confirmed': 'true', 'expected_version': 1}, content=b'expected')
    assert response.status_code == 202
    publication = (await shared_http['alice'].get(path + '/publications/' + response.json()['id'])).json()
    assert publication['status'] == 'failed'
    assert (await shared_http['alice'].get(path + '/materials')).json()['total'] == 0


async def test_source_change_during_copy_aborts_publish(shared_http, shared_id, r3_world, monkeypatch):
    """复制的是固定旧字节，但提交前源记录改变仍要求本人重新确认。"""
    path = '/v1/shared-matters/' + shared_id
    alice = shared_http['alice']
    file = (await alice.post('/v1/workspace/files', params={'name': '原件.txt', 'request_id': 'source'}, content=b'old')).json()
    objects = r3_world.platform.objects
    put = objects.put
    async def change_source(key, data, **kwargs):
        await put(key, data, **kwargs)
        if '/shared/' in key:
            updated = await alice.put('/v1/workspace/files/' + file['id'] + '/content',
                params={'request_id': 'source-change', 'version': file['version']}, content=b'new')
            assert updated.status_code == 200, updated.text
    monkeypatch.setattr(objects, 'put', change_source)
    response = await alice.post(path + '/materials', json={'request_id': 'publish-changing', 'confirmed': True,
        'expected_version': 1, 'source_item_id': file['id'], 'source_revision': 1})
    assert response.status_code == 202, response.text
    assert (await alice.get(path + '/publications/' + response.json()['id'])).json()['status'] == 'failed'
    assert (await alice.get(path + '/materials')).json()['total'] == 0


async def test_upload_limit_and_invalid_name_leave_no_intent(shared_http, shared_id, r3_world):
    """流式上限、空间预留和文件名称在意图落库前拒绝无效请求。"""
    from xuanpu_server.shared_matters.models import SharedMatterConfig
    r3_world.platform.shared_matters.config = SharedMatterConfig(enabled=True, tenants=('acme',), max_material_bytes=4, max_storage_bytes=4)
    path = '/v1/shared-matters/' + shared_id
    query = {'name': '小.txt', 'request_id': 'too-large', 'confirmed': 'true', 'expected_version': 1}
    assert (await shared_http['alice'].post(path + '/material-uploads', params=query, content=b'12345')).status_code == 400
    assert (await shared_http['alice'].post(path + '/material-uploads', params={**query, 'name': '../bad'}, content=b'1')).status_code == 400
    assert (await shared_http['alice'].get(path)).json()['version'] == 1
    ready = await shared_http['alice'].post(path + '/material-uploads', params={**query, 'request_id': 'first'}, content=b'1234')
    assert ready.status_code == 202
    second = await shared_http['alice'].post(path + '/material-uploads', params={**query, 'request_id': 'space', 'expected_version': 3}, content=b'1')
    assert second.status_code == 409, second.text

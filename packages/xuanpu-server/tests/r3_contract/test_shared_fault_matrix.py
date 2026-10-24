"""真实进程SIGKILL、持久对象恢复及慢读取撤权，不触碰生产容器。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest
from xuanpu_server.controlplane.postgres import PostgresControlPlane

from .test_shared_context import context_for, invite


async def spawn_worker(world, config, tmp_path, postgres_dsn):
    """子进程仅拿独占schema和合成用户，敏感配置不进命令行。"""
    store = world.platform.store
    if not isinstance(store, PostgresControlPlane):
        pytest.skip('真实进程持久化故障只对PostgreSQL执行')
    namespace = store._namespace
    config.update(dsn=postgres_dsn, schema=namespace.schema, prefix=namespace.prefix,
        tenant='acme', objects_dir=str(world.objects_dir), marker=str(tmp_path / 'copied.json'))
    path = tmp_path / ('worker-' + config['action'] + '.json')
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    script = Path(__file__).resolve().parents[4] / 'scripts/acceptance/r3_shared_worker.py'
    return await asyncio.create_subprocess_exec(sys.executable, str(script), '--config', str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)


async def test_sigkill_after_real_object_copy_recovers_once(shared_http, shared_id, r3_world, tmp_path, postgres_dsn, monkeypatch):
    """子进程已写对象后被SIGKILL，SQL仍pending；新恢复器只公开一个正确副本。"""
    worker = await spawn_worker(r3_world, {'action': 'copy', 'user_id': 'usr_alice',
        'shared_id': shared_id, 'version': 1}, tmp_path, postgres_dsn)
    try:
        async with asyncio.timeout(15):
            while not (tmp_path / 'copied.json').exists():
                if worker.returncode is not None:
                    _, stderr = await worker.communicate()
                    raise AssertionError('隔离worker提前退出：' + stderr.decode()[-1000:])
                await asyncio.sleep(.05)
        worker.kill()
        await asyncio.wait_for(worker.wait(), 10)
        assert worker.returncode == -9
        service = r3_world.platform.shared_matters
        async with service.store.transaction('acme') as tx:
            pending = next(iter(tx.shared.publications.values()))
            assert pending.status == 'pending' and not tx.shared.revisions
        assert hashlib.sha256(await r3_world.objects.get(pending.object_key)).hexdigest() == pending.expected_sha256
        from xuanpu_server.shared_matters.object_recovery import PublicationRecovery
        monkeypatch.setattr(service, 'now', lambda: pending.lease_until + 1)
        recovery = PublicationRecovery(r3_world.platform.shared_materials)
        assert await recovery.recover('acme') == 1
        assert await recovery.recover('acme') == 0
        items = (await shared_http['alice'].get('/v1/shared-matters/' + shared_id + '/materials')).json()
        assert items['total'] == 1 and len(items['items'][0]['revisions']) == 1
        url = '/v1/shared-matters/' + shared_id + '/materials/' + pending.material_id + '/content?revision=1'
        assert (await shared_http['alice'].get(url)).content == b'isolated-durable-copy-proof'
    finally:
        if worker.returncode is None:
            worker.kill()
            await worker.wait()


async def test_separate_cold_process_rejects_revoked_context(shared_http, shared_id, r3_world, tmp_path, postgres_dsn):
    """独立解释器从真实数据库装配服务，不能靠父进程的已阻塞缓存得到拒绝。"""
    granted = (await invite(shared_http, shared_id, 'process-reader', 1)).json()
    context = (await context_for(shared_http['bob'], shared_id, 'process-source', 2)).json()
    revoked = await shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/members/' + granted['id'] + '/revoke', json={
        'request_id': 'process-revoke', 'confirmed': True, 'expected_version': 2})
    assert revoked.status_code == 200
    worker = await spawn_worker(r3_world, {'action': 'read', 'user_id': 'usr_bob',
        'context_id': context['id']}, tmp_path, postgres_dsn)
    stdout, stderr = await asyncio.wait_for(worker.communicate(), 15)
    assert worker.returncode == 0, stderr.decode()[-1000:]
    assert json.loads(stdout)['code'] == 'shared_access_revoked'


async def test_slow_object_read_rechecks_revocation_before_http_bytes(shared_http, shared_id, r3_world, monkeypatch):
    """对象读取期间撤权，完整下载不得在最后一次鉴权之前交付字节。"""
    path = '/v1/shared-matters/' + shared_id
    response = await shared_http['alice'].post(path + '/material-uploads', params={
        'request_id': 'slow-copy', 'name': '慢读.txt', 'expected_version': 1, 'confirmed': 'true'}, content=b'no-post-revocation-bytes')
    assert response.status_code == 202
    granted = (await invite(shared_http, shared_id, 'slow-reader', 3)).json()
    original = r3_world.objects.get_limited
    started, release = asyncio.Event(), asyncio.Event()
    async def slow(key, **kwargs):
        """实际读取字节后暂停，随后在返回HTTP层前撤销来源。"""
        data = await original(key, **kwargs)
        started.set()
        await release.wait()
        return data
    monkeypatch.setattr(r3_world.objects, 'get_limited', slow)
    url = path + '/materials/' + response.json()['material_id'] + '/content?revision=1'
    task = asyncio.create_task(shared_http['bob'].get(url))
    await asyncio.wait_for(started.wait(), 10)
    revoked = await shared_http['alice'].post(path + '/members/' + granted['id'] + '/revoke', json={
        'request_id': 'slow-revoke', 'confirmed': True, 'expected_version': 4})
    assert revoked.status_code == 200
    release.set()
    denied = await asyncio.wait_for(task, 10)
    assert denied.status_code == 404 and b'no-post-revocation-bytes' not in denied.content

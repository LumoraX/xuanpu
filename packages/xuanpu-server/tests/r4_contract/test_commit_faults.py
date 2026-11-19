"""能力治理提交窗口的真实进程故障（R4 故障注入，仅 PostgreSQL）。

独立子进程用真实 PG 存储与真实服务代码执行一次写操作，停在提交窗口通知父进程，父进程 SIGKILL：

- 调用绑定：会话行已提交、绑定未写——证明两者不是同一事务；续聊因缺绑定被拒、不猜依据，
  重新建会话只产生一份绑定；
- 发布确认、撤销分配、撤销发布、验收观察：协调事务写完、提交前死亡——没有记录、回执与事件，
  安装不被置 ready、被取代的发布不被停；同一 request_id 重试恰好生效一次；
- 发布确认、验收观察的提交后死亡（响应丢失）：同一 request_id 重试回原结果，不产生第二份事实。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from xuanpu_server.tenancy import Principal

from .test_assignments import _assign, _epoch
from .test_capability_entrypoints import ENTRY, PACKAGE, _assigned, _say, _session
from .test_outcome_metrics import _capability, _seed
from .test_release_flow import _evaluate, _finished, _installed, _targets

WORKER = Path(__file__).resolve().parents[1] / 'data' / 'r4_commit_kill_worker.py'
ADMIN = {'role': 'tenant_admin', 'user': 'usr_admin'}


def _pg(world) -> Any:
    """控制面存储；内存后端跳过（真实进程终止只在 PostgreSQL 上有意义）。"""
    store = world.platform.store
    if not hasattr(store, '_pool'):
        pytest.skip('只在 PostgreSQL 后端验证真实进程终止')
    return store


async def _kill(world, dsn: str, tmp_path: Path, **config: Any) -> str:
    """启动工作进程，等它停在窗口后 SIGKILL；返回它报告的窗口名。"""
    namespace = world.platform.store._namespace
    ready = tmp_path / ('ready-' + secrets.token_hex(4))
    path = tmp_path / ('kill-' + secrets.token_hex(4) + '.json')
    path.write_text(json.dumps({'dsn': dsn, 'schema': namespace.schema, 'prefix': namespace.prefix,
                                'tenant': 'acme', 'ready': str(ready), **config}))
    path.chmod(0o600)
    process = await asyncio.create_subprocess_exec(sys.executable, str(WORKER), str(path),
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(60):
            while not ready.exists():
                if process.returncode is not None:
                    _, error = await process.communicate()
                    pytest.fail(error.decode()[-4000:])
                await asyncio.sleep(0.05)
    finally:
        process.kill()
        await process.wait()
    assert process.returncode is not None and process.returncode < 0, '须是被信号终止，而不是正常退出'
    return ready.read_text()


async def _count(store, table: str, where: str, *args: Any) -> int:
    """数某张表里本租户满足条件的行。"""
    async with store._pool.acquire() as conn:
        return int(await conn.fetchval(
            f'SELECT count(*) FROM {store._namespace.table(table)} WHERE tenant_id=$1 AND {where}', 'acme', *args))


async def _no_trace(store, request_id: str, kind: str) -> None:
    """提交前死亡：没有回执、没有这类事件。"""
    assert await _count(store, 'coordination_receipt', 'request_id=$2', request_id) == 0
    assert await _count(store, 'coordination_outbox', 'kind=$2', kind) == 0


async def _state(world):
    """权威能力状态（可重复读快照）。"""
    state, _ = await world.platform.organization.store.capability_snapshot('acme')
    return state


async def test_kill_between_session_and_binding(r4_http, r4_world, r4_ids, postgres_dsn, tmp_path, step):
    """会话行先提交、绑定后写：死在中间时会话在、绑定不在；续聊 403 且模型零调用；新会话只有一份绑定。"""
    store = _pg(r4_world)
    await _assigned(r4_http, r4_world, r4_ids)
    alice = Principal(role='tenant_member', tenant_id='acme', user_id='usr_alice', key_id=None)
    admission = r4_world.platform.capabilities.admission
    decision = await admission.admit(alice, PACKAGE, ENTRY, None)
    assert decision is not None
    orphan = 'ses_' + secrets.token_hex(16)
    where = await _kill(r4_world, postgres_dsn, tmp_path, mode='binding', stage='binding', role='tenant_member',
                        user='usr_alice', session_id=orphan, decision=asdict(decision))
    assert where == 'before-binding'
    assert await store.get_session('acme', orphan) is not None, '会话行是独立提交的'
    assert await admission.binding('acme', 'session', orphan) is None
    before = len(r4_world.models.requests)
    assert (await _say(r4_http['alice'], orphan)).status_code == 403
    assert len(r4_world.models.requests) == before, '缺绑定不能先调模型再拒'
    retried = await _session(r4_http['alice'])
    assert retried.status_code == 201, retried.text
    fresh = retried.json()['session_id']
    assert fresh != orphan and await admission.binding('acme', 'session', fresh) is not None
    assert await _count(store, 'capability_binding', "kind='session'") == 1
    r4_world.models.script(step(text='好的。'))
    assert (await _say(r4_http['alice'], fresh)).status_code == 200


async def _evaluated(r4_http, r4_world, r4_ids, step) -> tuple[str, str, str]:
    """安装（staged）、评测通过、Alice 有分配；返回能力、安装与评测 id。"""
    admin = r4_http['admin']
    cap, installation = await _installed(admin, r4_ids)
    r4_world.models.script(step(text='核对完成。'))
    evaluation = (await _evaluate(admin, cap, installation)).json()['id']
    assert (await _finished(admin, evaluation))['status'] == 'passed'
    selector = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    assert (await _assign(admin, cap, selector)).status_code == 201
    return cap, installation, evaluation


async def _release_body(r4_http, r4_world, cap, installation, evaluation, request_id) -> dict:
    """经预览接口拿摘要与需取代的发布，拼出确认体。"""
    body = {'installation_id': installation, 'evaluation_id': evaluation,
            'targets': await _targets(r4_world, ['usr_alice'])}
    preview = await r4_http['admin'].post(f'/v1/capabilities/{cap}/release-previews', json=body)
    assert preview.status_code == 200, preview.text
    return {**body, 'request_id': request_id, 'preview_digest': preview.json()['digest'],
            'supersedes': preview.json()['supersedes'], 'confirmed': True}


async def test_kill_before_release_commit(r4_http, r4_world, r4_ids, postgres_dsn, tmp_path, step):
    """首次发布与取代发布都死在提交前：无发布、安装仍 staged、旧发布仍生效；同一 request_id 重试恰好一个。"""
    store = _pg(r4_world)
    cap, installation, evaluation = await _evaluated(r4_http, r4_world, r4_ids, step)
    admin = r4_http['admin']
    first = await _release_body(r4_http, r4_world, cap, installation, evaluation, 'rel-1')
    assert await _kill(r4_world, postgres_dsn, tmp_path, mode='release', stage='before', capability_id=cap,
                       body=first, **ADMIN) == 'before-commit'
    state = await _state(r4_world)
    assert state.releases == {} and state.installations[installation].status == 'staged'
    await _no_trace(store, 'rel-1', 'capability.released')
    assert await _count(store, 'coordination_outbox', "kind='capability.state_changed'") == 0
    released = await admin.post(f'/v1/capabilities/{cap}/releases', json=first)
    assert released.status_code == 201, released.text
    old = released.json()
    state = await _state(r4_world)
    assert list(state.releases) == [old['id']] and state.installations[installation].status == 'ready'
    second = await _release_body(r4_http, r4_world, cap, installation, evaluation, 'rel-2')
    assert second['supersedes'] == [old['id']]
    await _kill(r4_world, postgres_dsn, tmp_path, mode='release', stage='before', capability_id=cap,
                body=second, **ADMIN)
    state = await _state(r4_world)
    assert list(state.releases) == [old['id']]
    assert (state.releases[old['id']].status, state.releases[old['id']].version) == ('active', old['version'])
    await _no_trace(store, 'rel-2', 'capability.release_retired')
    replaced = await admin.post(f'/v1/capabilities/{cap}/releases', json=second)
    assert replaced.status_code == 201, replaced.text
    state = await _state(r4_world)
    assert len(state.releases) == 2 and state.releases[old['id']].status == 'retired'
    assert await _count(store, 'coordination_outbox', "kind='capability.released'") == 2
    assert await _count(store, 'coordination_outbox', "kind='capability.release_retired'") == 1


async def test_kill_after_release_commit(r4_http, r4_world, r4_ids, postgres_dsn, tmp_path, step):
    """提交后、响应前死亡：发布已生效；同一 request_id 重试回原发布，不产生第二个。"""
    store = _pg(r4_world)
    cap, installation, evaluation = await _evaluated(r4_http, r4_world, r4_ids, step)
    body = await _release_body(r4_http, r4_world, cap, installation, evaluation, 'rel-lost')
    assert await _kill(r4_world, postgres_dsn, tmp_path, mode='release', stage='after', capability_id=cap,
                       body=body, **ADMIN) == 'after-commit'
    state = await _state(r4_world)
    assert len(state.releases) == 1 and state.installations[installation].status == 'ready'
    replay = await r4_http['admin'].post(f'/v1/capabilities/{cap}/releases', json=body)
    assert replay.status_code == 201, replay.text
    assert replay.json()['id'] == next(iter(state.releases))
    assert len((await _state(r4_world)).releases) == 1
    assert await _count(store, 'coordination_outbox', "kind='capability.released'") == 1


async def test_kill_before_revocation_commits(r4_http, r4_world, r4_ids, postgres_dsn, tmp_path):
    """撤销分配、撤销发布死在提交前：状态、版本与代次都不变；重试各生效一次（代次只加一）。"""
    store = _pg(r4_world)
    cap, assignment = await _assigned(r4_http, r4_world, r4_ids)
    admin = r4_http['admin']
    body = {'request_id': 'rv-kill', 'expected_version': assignment['version']}
    await _kill(r4_world, postgres_dsn, tmp_path, mode='assignment_revoke', stage='before',
                assignment_id=assignment['id'], body=body, **ADMIN)
    item = (await _state(r4_world)).assignments[assignment['id']]
    assert (item.status, item.epoch, item.version) == ('active', assignment['epoch'], assignment['version'])
    await _no_trace(store, 'rv-kill', 'capability.assignment_revoked')
    revoked = await admin.post(f"/v1/capability-assignments/{assignment['id']}/revoke", json=body)
    assert revoked.status_code == 200, revoked.text
    assert (revoked.json()['status'], revoked.json()['epoch']) == ('revoked', assignment['epoch'] + 1)
    again = await admin.post(f"/v1/capability-assignments/{assignment['id']}/revoke", json=body)
    assert again.json() == revoked.json()
    assert await _count(store, 'coordination_outbox', "kind='capability.assignment_revoked'") == 1

    release = next(r for r in (await _state(r4_world)).releases.values() if r.capability_id == cap)
    body = {'request_id': 'rr-kill', 'expected_version': release.version}
    await _kill(r4_world, postgres_dsn, tmp_path, mode='release_revoke', stage='before',
                release_id=release.id, body=body, **ADMIN)
    kept = (await _state(r4_world)).releases[release.id]
    assert (kept.status, kept.epoch, kept.version) == ('active', release.epoch, release.version)
    await _no_trace(store, 'rr-kill', 'capability.release_revoked')
    changed = await admin.post(f'/v1/capability-releases/{release.id}/revoke', json=body)
    assert changed.status_code == 200, changed.text
    assert (changed.json()['status'], changed.json()['epoch']) == ('revoked', release.epoch + 1)
    assert await _count(store, 'coordination_outbox', "kind='capability.release_revoked'") == 1


async def test_kill_around_observation_commit(r4_http, r4_world, postgres_dsn, tmp_path):
    """验收观察提交前死亡无观察、重试一次；提交后死亡已登记、重试回原观察。"""
    store = _pg(r4_world)
    await _capability(r4_world)
    for index in range(2):
        await _seed(r4_world, 'usr_alice', index, 'succeeded', 1000.0 + index)
    admin = r4_http['admin']
    samples = [f'task_usr_alice_{i}' for i in range(2)]
    created = await admin.post('/v1/admin/acceptance-batches', json={
        'request_id': 'b1', 'capability_id': 'cap_x', 'capability_revision': 1, 'name': '故障批',
        'criteria': '金额与预期一致', 'samples': samples})
    assert created.status_code == 201, created.text
    batch = created.json()['id']
    path = f'/v1/acceptance-batches/{batch}/observations'
    first = {'request_id': 'o-before', 'task_id': samples[0], 'verdict': 'passed', 'evidence_ref': 'e.json#0'}
    await _kill(r4_world, postgres_dsn, tmp_path, mode='observe', stage='before', batch_id=batch,
                body=first, **ADMIN)
    assert (await _state(r4_world)).observations == {}
    await _no_trace(store, 'o-before', 'acceptance.observed')
    observed = await admin.post(path, json=first)
    assert observed.status_code == 201, observed.text
    assert (await admin.post(path, json=first)).json() == observed.json()
    second = {'request_id': 'o-after', 'task_id': samples[1], 'verdict': 'failed', 'evidence_ref': 'e.json#1'}
    assert await _kill(r4_world, postgres_dsn, tmp_path, mode='observe', stage='after', batch_id=batch,
                       body=second, **ADMIN) == 'after-commit'
    assert len((await _state(r4_world)).observations) == 2
    replay = await admin.post(path, json=second)
    assert replay.status_code == 201, replay.text
    assert len((await _state(r4_world)).observations) == 2
    assert await _count(store, 'coordination_outbox', "kind='acceptance.observed'") == 2
    detail = (await admin.get(f'/v1/acceptance-batches/{batch}')).json()
    assert detail['counts'] == {'passed': 1, 'failed': 1, 'blocked': 0, 'not_checked': 0}

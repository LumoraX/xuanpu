"""隔离评测与按员工发布（R4-C）：真实接口走完 评测→预览→确认发布→停止/撤销/回退。

- 套件摘要须等于交付描述；未通过或证据与当前配置不符的评测不能授权发布；
- 发布只覆盖名单里的员工（还须有分配）；重叠须明确取代；ready 只能经发布确立；
- 撤销让已绑定的会话也不能继续，停止只拒新受理；评测结束后评测会话不能被续用。
内存与 PG 各跑一遍。
"""
from __future__ import annotations

import asyncio
import copy

from .seed import SUITE
from .test_assignments import _assign, _epoch
from .test_capability_entrypoints import _say, _session
from .test_installation import register


async def _installed(admin, ids) -> tuple[str, str]:
    """登记、预检、安装（staged）；返回能力与安装 id。"""
    cap = await register(admin, ids)
    preview = await admin.post(f'/v1/capabilities/{cap}/install-previews', json={'request_id': 'pv', 'revision': 1})
    installed = await admin.post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': 'in', 'preview_id': preview.json()['id'], 'expected_version': 0, 'confirmed': True})
    assert installed.status_code == 201, installed.text
    return cap, installed.json()['id']


async def _evaluate(admin, cap: str, installation: str, request_id: str = 'ev', suite: dict = SUITE):
    """发起评测；返回响应。"""
    return await admin.post('/v1/capability-evaluations', json={
        'request_id': request_id, 'capability_id': cap, 'installation_id': installation, 'suite': suite})


async def _finished(admin, evaluation_id: str) -> dict:
    """等评测结束。"""
    async with asyncio.timeout(20):
        while True:
            body = (await admin.get(f'/v1/capability-evaluations/{evaluation_id}')).json()
            if body['status'] != 'running':
                return body
            await asyncio.sleep(0.05)


async def _targets(world, users: list[str]) -> list[dict]:
    """发布名单（含当前代次）。"""
    return [{'user_id': u, 'user_epoch': await _epoch(world, u)} for u in users]


async def _release(admin, cap, installation, evaluation, targets, request_id='rel', supersedes=()):
    """预览后按摘要确认发布；返回 (预览, 发布响应)。"""
    body = {'installation_id': installation, 'evaluation_id': evaluation, 'targets': targets}
    preview = await admin.post(f'/v1/capabilities/{cap}/release-previews', json=body)
    if preview.status_code != 200:
        return preview, None
    released = await admin.post(f'/v1/capabilities/{cap}/releases', json={
        **body, 'request_id': request_id, 'preview_digest': preview.json()['digest'],
        'supersedes': list(supersedes), 'confirmed': True})
    return preview, released


async def test_evaluation_binds_to_the_declared_suite(r4_http, r4_world, r4_ids, step):
    """套件摘要不符 409；相符则后台运行，按实际回复判定通过或失败。"""
    admin = r4_http['admin']
    cap, installation = await _installed(admin, r4_ids)
    other = copy.deepcopy(SUITE)
    other['cases'][0]['input'] = '换了一句'
    assert (await _evaluate(admin, cap, installation, 'bad', other)).status_code == 409
    r4_world.models.script(step(text='材料核对完成。'))
    started = await _evaluate(admin, cap, installation)
    assert started.status_code == 202, started.text
    assert (await _finished(admin, started.json()['id']))['status'] == 'passed'
    r4_world.models.script(step(text='我不确定。'))
    failing = await _evaluate(admin, cap, installation, 'ev2')
    result = await _finished(admin, failing.json()['id'])
    assert result['status'] == 'failed' and '核对完成' in result['cases'][0]['reasons'][0]


async def test_release_requires_passing_matching_evidence(r4_http, r4_world, r4_ids, step):
    """未通过的评测不能发布；预览摘要不符 409；安装不能直接置 ready；发布后只覆盖名单里有分配的员工。"""
    admin = r4_http['admin']
    cap, installation = await _installed(admin, r4_ids)
    r4_world.models.script(step(text='我不确定。'))
    failed = (await _evaluate(admin, cap, installation, 'evf')).json()['id']
    await _finished(admin, failed)
    targets = await _targets(r4_world, ['usr_alice', 'usr_bob'])
    preview, _ = await _release(admin, cap, installation, failed, targets)
    assert preview.status_code == 409
    r4_world.models.script(step(text='核对完成。'))
    passed = (await _evaluate(admin, cap, installation, 'evp')).json()['id']
    await _finished(admin, passed)
    ready = await admin.post(f'/v1/installations/{installation}/state', json={
        'request_id': 'st', 'expected_version': 1, 'status': 'ready'})
    assert ready.status_code == 409
    wrong = await admin.post(f'/v1/capabilities/{cap}/releases', json={
        'installation_id': installation, 'evaluation_id': passed, 'targets': targets,
        'request_id': 'x', 'preview_digest': '0' * 64, 'supersedes': [], 'confirmed': True})
    assert wrong.status_code == 409
    selector = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    assert (await _assign(admin, cap, selector)).status_code == 201
    _, released = await _release(admin, cap, installation, passed, await _targets(r4_world, ['usr_alice']))
    assert released.status_code == 201, released.text
    alice = (await r4_http['alice'].get('/v1/me/capabilities')).json()['capabilities']
    assert [c['capability_id'] for c in alice] == [cap]
    assert (await r4_http['bob'].get('/v1/me/capabilities')).json()['capabilities'] == []


async def test_overlap_supersede_retire_and_revoke(r4_http, r4_world, r4_ids, step):
    """重叠须明确取代；停止只拒新建、已绑定可续；撤销后已绑定的也不能继续。"""
    admin = r4_http['admin']
    cap, installation = await _installed(admin, r4_ids)
    r4_world.models.script(step(text='核对完成。'))
    evaluation = (await _evaluate(admin, cap, installation)).json()['id']
    await _finished(admin, evaluation)
    selector = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    await _assign(admin, cap, selector)
    alice = await _targets(r4_world, ['usr_alice'])
    _, first = await _release(admin, cap, installation, evaluation, alice, 'r1')
    _, clash = await _release(admin, cap, installation, evaluation, alice, 'r2')
    assert clash.status_code == 409
    _, second = await _release(admin, cap, installation, evaluation, alice, 'r3', [first.json()['id']])
    assert second.status_code == 201, second.text
    session = (await _session(r4_http['alice'])).json()['session_id']
    retired = await admin.post(f"/v1/capability-releases/{second.json()['id']}/retire",
                               json={'request_id': 'ret', 'expected_version': 1})
    assert retired.status_code == 200 and retired.json()['status'] == 'retired'
    assert (await _session(r4_http['alice'])).status_code == 404
    r4_world.models.script(step(text='收尾。'))
    assert (await _say(r4_http['alice'], session)).status_code == 200
    _, third = await _release(admin, cap, installation, evaluation, alice, 'r4')
    session = (await _session(r4_http['alice'])).json()['session_id']
    revoked = await admin.post(f"/v1/capability-releases/{third.json()['id']}/revoke",
                               json={'request_id': 'rev', 'expected_version': 1})
    assert revoked.json()['status'] == 'revoked' and revoked.json()['epoch'] == 2
    assert (await _say(r4_http['alice'], session)).status_code == 403


async def test_evaluation_sessions_cannot_be_reused(r4_http, r4_world, r4_ids, step):
    """评测结束后，评测会话不能再被续用（评测绑定只认进行中且列名的评测）。"""
    admin = r4_http['admin']
    cap, installation = await _installed(admin, r4_ids)
    r4_world.models.script(step(text='核对完成。'))
    evaluation = (await _evaluate(admin, cap, installation)).json()['id']
    await _finished(admin, evaluation)
    state, _ = await r4_world.platform.organization.store.capability_snapshot('acme')
    [session] = state.evaluations[evaluation].sessions
    assert (await _say(admin, session)).status_code in (403, 404, 409)

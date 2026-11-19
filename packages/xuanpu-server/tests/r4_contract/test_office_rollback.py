"""升级与回退旅程（R4-E1 一部分）：发布修订 1 → 升级到修订 2（新包版本）→ 回退到修订 1。
新会话随当前发布换版本，已受理的会话始终按原版本继续；回退是用旧修订的配置与评测证据再发布一次，
不改写旧会话。内存与 PG。"""
from __future__ import annotations

from .seed import delivery, package_bytes
from .test_assignments import _epoch
from .test_capability_entrypoints import _say, _session
from .test_release_flow import _evaluate, _finished, _installed, _release, _targets


async def _binding_version(world, session_id: str) -> str:
    """会话固定的包版本。"""
    binding = await world.platform.capabilities.admission.binding('acme', 'session', session_id)
    assert binding is not None
    return binding.package_version


async def test_upgrade_then_rollback_keeps_old_sessions_on_their_version(r4_http, r4_world, r4_ids, step):
    """升级窗口里旧发布照常可用；升级后新会话用 1.1.0、旧会话仍是 1.0.0；回退后新会话回到 1.0.0、1.1.0 的会话不受影响。"""
    admin = r4_http['admin']
    cap, installation = await _installed(admin, r4_ids)
    r4_world.models.script(step(text='核对完成。'))
    first_eval = (await _evaluate(admin, cap, installation, 'ev1')).json()['id']
    assert (await _finished(admin, first_eval))['status'] == 'passed'
    selector = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    assert (await admin.post(f'/v1/capabilities/{cap}/assignments', json={
        'request_id': 'as', 'selector': selector, 'allowed_revisions': [1]})).status_code == 201
    alice = await _targets(r4_world, ['usr_alice'])
    _, first = await _release(admin, cap, installation, first_eval, alice, 'rel1')
    assert first is not None and first.status_code == 201, first.text
    old_session = (await _session(r4_http['alice'], None)).json()['session_id']
    assert await _binding_version(r4_world, old_session) == '1.0.0'

    # 升级：发布 1.1.0 归档，登记修订 2，重新预检并安装，评测后取代修订 1 的发布
    archive = package_bytes(r4_world.package_dir / 'r4-up', '1.1.0')
    published = await r4_world.platform.packages.publish(r4_world.admin, archive)
    registered = await admin.post('/v1/capabilities', json={
        'request_id': 'register-2', 'delivery': delivery(published.digest, '1.1.0')})
    assert registered.status_code == 201 and registered.json()['revision'] == 2, registered.text
    assert (await admin.post(f'/v1/capabilities/{cap}/assignments', json={
        'request_id': 'as2', 'selector': selector, 'allowed_revisions': [1, 2]})).status_code == 201
    preview = await admin.post(f'/v1/capabilities/{cap}/install-previews', json={'request_id': 'pv2', 'revision': 2})
    assert preview.status_code == 201, preview.text
    current = (await admin.get('/v1/capabilities')).json()['installations'][0]
    upgraded = await admin.post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': 'in2', 'preview_id': preview.json()['id'], 'expected_version': current['version'],
        'confirmed': True})
    assert upgraded.status_code == 201, upgraded.text
    # 升级窗口：新配置还没评测发布，修订 1 的发布照常可用（换配置不撤销已生效的发布）
    during = await _session(r4_http['alice'], None)
    assert during.status_code == 201, during.text
    assert await _binding_version(r4_world, during.json()['session_id']) == '1.0.0'
    r4_world.models.script(step(text='核对完成。'))
    second_eval = (await _evaluate(admin, cap, installation, 'ev2')).json()['id']
    assert (await _finished(admin, second_eval))['status'] == 'passed'
    _, second = await _release(admin, cap, installation, second_eval, alice, 'rel2', [first.json()['id']])
    assert second is not None and second.status_code == 201, second.text
    new_session = (await _session(r4_http['alice'], None)).json()['session_id']
    assert await _binding_version(r4_world, new_session) == '1.1.0'
    r4_world.models.script(step(text='旧会话收尾。'))
    assert (await _say(r4_http['alice'], old_session)).status_code == 200
    assert await _binding_version(r4_world, old_session) == '1.0.0'

    # 回退：用修订 1 的配置与评测再发布一次（取代修订 2 的发布）
    preview_back, back = await _release(admin, cap, installation, first_eval, alice, 'rel3', [second.json()['id']])
    assert back is not None and back.status_code == 201, preview_back.text
    rolled = (await _session(r4_http['alice'], None)).json()['session_id']
    assert await _binding_version(r4_world, rolled) == '1.0.0'
    r4_world.models.script(step(text='新版会话收尾。'))
    assert (await _say(r4_http['alice'], new_session)).status_code == 200
    assert await _binding_version(r4_world, new_session) == '1.1.0'

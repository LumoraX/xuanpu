"""全入口统一鉴权与版本固定（R4-B1）：显式建会话、续聊、对话目录、旧激活接口都走同一能力决策；
撤权、停用、缺绑定都在续跑时被拒；开关关闭不新建、已绑定的可收尾。内存与 PG 各跑一遍。"""
from __future__ import annotations

from xuanpu_server.capabilities.config import CapabilityDeliveryConfig
from xuanpu_server.tenancy import Principal

from .test_assignments import _assign, _epoch, _ready

PACKAGE, VERSION, ENTRY = 'office-review', '1.0.0', 'review'


async def _assigned(r4_http, r4_world, r4_ids) -> tuple[str, dict]:
    """能力 ready 并只分配给 Alice；返回能力 id 与分配记录。"""
    admin = r4_http['admin']
    cap = await _ready(admin, r4_world, r4_ids)
    selector = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    assigned = await _assign(admin, cap, selector)
    assert assigned.status_code == 201, assigned.text
    return cap, assigned.json()


async def _session(client, version: str | None = VERSION):
    """显式建会话。"""
    body = {'package': PACKAGE, 'entry': ENTRY}
    if version is not None:
        body['version'] = version
    return await client.post('/v1/sessions', json=body)


async def _say(client, session_id: str):
    """续聊一句（读完事件流）。"""
    return await client.post(f'/v1/sessions/{session_id}/messages', json={'text': '继续'})


async def test_explicit_session_requires_assignment(r4_http, r4_world, r4_ids):
    """知道包名与精确版本也不能绕过分配；机器身份没有员工委托；版本不符 404；Alice 建会话即固定绑定。"""
    await _assigned(r4_http, r4_world, r4_ids)
    assert (await _session(r4_http['bob'])).status_code == 404
    assert (await _session(r4_http['machine'])).status_code == 404
    assert (await _session(r4_http['alice'], '9.9.9')).status_code == 404
    created = await _session(r4_http['alice'], None)
    assert created.status_code == 201, created.text
    session_id = created.json()['session_id']
    binding = await r4_world.platform.capabilities.admission.binding('acme', 'session', session_id)
    assert binding is not None and binding.user_id == 'usr_alice' and binding.package_version == VERSION


async def test_managed_package_cannot_use_legacy_activation(r4_http, r4_world, r4_ids):
    """旧激活接口与「发布并激活」都不能绕过发布门禁（409）；不激活的发布照常可用。"""
    await _assigned(r4_http, r4_world, r4_ids)
    activate = await r4_http['admin'].post(f'/v1/packages/{PACKAGE}/activate', json={'version': VERSION})
    assert activate.status_code == 409
    from .seed import package_bytes

    archive = package_bytes(r4_world.package_dir / 'r4-v2', '1.1.0')
    blocked = await r4_http['admin'].post('/v1/packages?activate=true', content=archive)
    assert blocked.status_code == 409
    assert [r.version for r in await r4_world.platform.packages.list_versions('acme', PACKAGE)] == [VERSION]
    plain = await r4_http['admin'].post('/v1/packages', content=archive)
    assert plain.status_code == 201, plain.text


async def test_continuation_rechecks_current_eligibility(r4_http, r4_world, r4_ids, step):
    """续聊对照原依据：撤销分配后拒绝；不会换成别的授权继续。"""
    cap, assignment = await _assigned(r4_http, r4_world, r4_ids)
    session_id = (await _session(r4_http['alice'])).json()['session_id']
    r4_world.models.script(step(text='好的。'))
    assert (await _say(r4_http['alice'], session_id)).status_code == 200
    revoked = await r4_http['admin'].post(f"/v1/capability-assignments/{assignment['id']}/revoke",
                                          json={'request_id': 'rv', 'expected_version': 1})
    assert revoked.status_code == 200
    # 再给一项新分配：原会话仍按原依据判定，不被新授权「复活」
    selector = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    assert (await _assign(r4_http['admin'], cap, selector, 'again')).status_code == 201
    assert (await _say(r4_http['alice'], session_id)).status_code == 403


async def test_installation_state_governs_continuation(r4_http, r4_world, r4_ids, step):
    """draining：不新建、原会话可收尾；disabled：原会话也不能继续。"""
    await _assigned(r4_http, r4_world, r4_ids)
    session_id = (await _session(r4_http['alice'])).json()['session_id']
    store = r4_world.platform.organization.store

    async def set_status(status: str) -> None:
        async with store.transaction('acme') as tx:
            item = next(iter(tx.state.capabilities.installations.values()))
            tx.state.capabilities.installations[item.id] = item.model_copy(
                update={'status': status, 'version': item.version + 1})

    await set_status('draining')
    assert (await _session(r4_http['alice'])).status_code == 404
    r4_world.models.script(step(text='收尾。'))
    assert (await _say(r4_http['alice'], session_id)).status_code == 200
    await set_status('disabled')
    assert (await _say(r4_http['alice'], session_id)).status_code == 403


async def test_managed_work_without_binding_is_refused(r4_http, r4_world, r4_ids, step):
    """纳管之前建的会话（没有绑定）：纳管后不能继续，不替它猜分配。"""
    created = await _session(r4_http['alice'])
    assert created.status_code == 201  # 尚未登记为能力：兼容路径
    await _assigned(r4_http, r4_world, r4_ids)
    assert (await _say(r4_http['alice'], created.json()['session_id'])).status_code == 403


async def test_router_catalog_is_per_employee(r4_http, r4_world, r4_ids):
    """对话目录：纳管入口只给有分配的员工，未纳管入口照旧。"""
    await _assigned(r4_http, r4_world, r4_ids)
    assistant = r4_world.platform.assistant

    def who(user: str) -> Principal:
        return Principal(role='tenant_member', tenant_id='acme', user_id=user, key_id=None)

    alice = {(e['package'], e['entry']) for e in await assistant._catalog('acme', who('usr_alice'))}
    bob = {(e['package'], e['entry']) for e in await assistant._catalog('acme', who('usr_bob'))}
    assert (PACKAGE, ENTRY) in alice and (PACKAGE, ENTRY) not in bob


async def test_switch_off_blocks_new_but_lets_bound_work_finish(r4_http, r4_world, r4_ids, step):
    """开关关闭：纳管能力不能新建（不退回旧全租户入口）；已绑定的会话按当前资格收尾。"""
    await _assigned(r4_http, r4_world, r4_ids)
    session_id = (await _session(r4_http['alice'])).json()['session_id']
    r4_world.platform.capabilities.config = CapabilityDeliveryConfig()
    assert (await _session(r4_http['alice'])).status_code == 403
    r4_world.models.script(step(text='收尾。'))
    assert (await _say(r4_http['alice'], session_id)).status_code == 200


async def test_revocation_after_turn_start_blocks_the_model_call(r4_http, r4_world, r4_ids, step, monkeypatch):
    """轮开始时的复核已过、随后才撤销分配：这一轮的模型请求在发出前被拒，模型零调用。"""
    _, assignment = await _assigned(r4_http, r4_world, r4_ids)
    session_id = (await _session(r4_http['alice'])).json()['session_id']
    r4_world.models.script(step(text='好的。'))
    assert (await _say(r4_http['alice'], session_id)).status_code == 200
    before = len(r4_world.models.requests)

    async def passed(record):
        """模拟撤销恰好落在轮开始复核之后。"""

    monkeypatch.setattr(r4_world.platform.sessions, '_capability_guard', passed)
    revoked = await r4_http['admin'].post(f"/v1/capability-assignments/{assignment['id']}/revoke",
                                          json={'request_id': 'rv-mid', 'expected_version': 1})
    assert revoked.status_code == 200
    r4_world.models.script(step(text='不应发出。'))
    response = await _say(r4_http['alice'], session_id)
    assert '不应发出' not in response.text
    assert len(r4_world.models.requests) == before, '撤销后这一轮不能再向模型发请求'


async def test_drain_preview_counts_unbound_inflight_work(r4_http, r4_world, r4_ids):
    """登记前：这个包还在进行、没有能力依据的会话被数出来；登记后同样给出（续跑会被拒）；员工不能看。"""
    from .test_assignments import register

    started = await _session(r4_http['alice'], None)
    assert started.status_code == 201, started.text
    admin = r4_http['admin']
    before = (await admin.get('/v1/admin/capability-drain', params={'package': PACKAGE})).json()
    assert before['managed'] is False and before['unbound_sessions'] == 1 and before['truncated'] is False
    assert (await admin.get('/v1/admin/capability-drain', params={'package': 'other'})).json()['unbound_sessions'] == 0
    await register(admin, r4_ids)
    after = (await admin.get('/v1/admin/capability-drain', params={'package': PACKAGE})).json()
    assert after['managed'] is True and after['unbound_sessions'] == 1
    assert started.json()['session_id'] not in str(after)
    assert (await r4_http['alice'].get('/v1/admin/capability-drain', params={'package': PACKAGE})).status_code == 403

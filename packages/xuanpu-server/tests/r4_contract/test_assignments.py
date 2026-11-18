"""到人能力分配与当前资格（R4-B0）：直接到人不需要部门；岗位/部门经真实任职；撤销、过期、
代次与结构版本变化都让资格失效；跨租户、非管理员、机器身份都被拒。内存与 PG 各跑一遍。"""
from __future__ import annotations

import asyncio
import time

from xuanpu_server.organization.models import Membership, OrgUnit, Position
from xuanpu_server.organization.store import OrganizationRepository

from .test_installation import register


async def _ready(client, world, ids) -> str:
    """登记、预检、安装，再用测试专用写入把安装置为 ready（生产里 ready 须经 R4-C 发布门禁）。"""
    cap = await register(client, ids)
    preview = await client.post(f'/v1/capabilities/{cap}/install-previews',
                                json={'request_id': 'preview', 'revision': 1})
    assert preview.status_code == 201, preview.text
    installed = await client.post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': 'install', 'preview_id': preview.json()['id'], 'expected_version': 0,
        'confirmed': True})
    assert installed.status_code == 201, installed.text
    await seed_release(world, cap, installed.json()['id'], ['usr_alice', 'usr_bob'])
    return cap


async def seed_release(world, cap: str, installation_id: str, users: list[str]) -> str:
    """测试专用写入：把安装置为 ready，并写一份已通过的评测与覆盖这些员工的生效发布。

    生产里这两步只能经评测与发布接口（R4-C，另有专门用例）；这里只为单独检验分配语义。
    """
    from xuanpu_server.capabilities.models import (
        CapabilityEvaluation,
        CapabilityRelease,
        ReleaseTarget,
    )

    from .seed import SUITE

    targets = [ReleaseTarget(user_id=u, user_epoch=await _epoch(world, u)) for u in users]
    store = world.platform.organization.store
    async with store.transaction('acme') as tx:
        state = tx.state.capabilities
        item = state.installations[installation_id]
        state.installations[item.id] = item.model_copy(update={'status': 'ready', 'version': item.version + 1})
        config = state.configurations[item.config_id]
        evaluation = CapabilityEvaluation(
            tenant_id='acme', id='cev_seed_' + cap, created_at=time.time(), capability_id=cap,
            capability_revision=config.revision, installation_id=item.id, config_id=config.id,
            config_digest=config.digest, suite_digest='0' * 64, suite=SUITE, token_budget=100_000,
            status='passed', finished_at=time.time())
        state.evaluations[evaluation.id] = evaluation
        release = CapabilityRelease(
            tenant_id='acme', id='crl_seed_' + cap, created_at=time.time(), capability_id=cap,
            capability_revision=config.revision, installation_id=item.id, config_id=config.id,
            config_digest=config.digest, evaluation_id=evaluation.id, targets=tuple(targets))
        state.releases[release.id] = release
    return release.id


async def _epoch(world, user: str) -> int:
    """员工当前生命周期代次。"""
    record = await world.platform.identity.store.get_user('acme', user)
    return int(record.lifecycle_epoch)


async def _assign(client, cap: str, selector: dict, request_id: str = 'assign', **extra):
    """经产品接口分配。"""
    return await client.post(f'/v1/capabilities/{cap}/assignments', json={
        'request_id': request_id, 'selector': selector, 'allowed_revisions': [1], **extra})


async def _mine(client) -> list[str]:
    """本人可新受理的能力。"""
    response = await client.get('/v1/me/capabilities')
    assert response.status_code == 200, response.text
    return [c['capability_id'] for c in response.json()['capabilities']]


async def test_direct_assignment_needs_no_department(r4_http, r4_world, r4_ids):
    """没有部门的 Alice 可以直接分配；只有她看得到，Bob 看不到；回执幂等。"""
    admin = r4_http['admin']
    cap = await _ready(admin, r4_world, r4_ids)
    selector = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    first = await _assign(admin, cap, selector)
    assert first.status_code == 201, first.text
    assert first.json()['grants'] == 'capability_use_only'
    again = await _assign(admin, cap, selector)
    assert again.json() == first.json()
    assert await _mine(r4_http['alice']) == [cap]
    assert await _mine(r4_http['bob']) == []


async def test_not_ready_installation_grants_nothing(r4_http, r4_world, r4_ids):
    """只有 staged 安装：有分配也不能运行。"""
    admin = r4_http['admin']
    cap = await register(admin, r4_ids)
    selector = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    assert (await _assign(admin, cap, selector)).status_code == 201
    assert await _mine(r4_http['alice']) == []


async def test_revoke_is_cas_final_and_immediate(r4_http, r4_world, r4_ids):
    """按版本撤销；撤销后立即不可用；不能再撤或改写；版本不符 409。"""
    admin = r4_http['admin']
    cap = await _ready(admin, r4_world, r4_ids)
    selector = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    assigned = (await _assign(admin, cap, selector)).json()
    stale = await admin.post(f"/v1/capability-assignments/{assigned['id']}/revoke",
                             json={'request_id': 'r0', 'expected_version': 9})
    assert stale.status_code == 409
    revoked = await admin.post(f"/v1/capability-assignments/{assigned['id']}/revoke",
                               json={'request_id': 'r1', 'expected_version': 1})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()['status'] == 'revoked' and revoked.json()['epoch'] == 2
    assert await _mine(r4_http['alice']) == []
    twice = await admin.post(f"/v1/capability-assignments/{assigned['id']}/revoke",
                             json={'request_id': 'r2', 'expected_version': 2})
    assert twice.status_code == 409


async def test_expiry_and_revision_scope(r4_http, r4_world, r4_ids):
    """到期即失效；分配的修订范围不含安装所用修订时不可用；不存在的修订 400。"""
    admin = r4_http['admin']
    cap = await _ready(admin, r4_world, r4_ids)
    selector = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    soon = await _assign(admin, cap, selector, valid_until=time.time() + 0.5)
    assert soon.status_code == 201
    assert await _mine(r4_http['alice']) == [cap]
    await asyncio.sleep(0.7)
    assert await _mine(r4_http['alice']) == []
    missing = await admin.post(f'/v1/capabilities/{cap}/assignments', json={
        'request_id': 'bad', 'selector': selector, 'allowed_revisions': [7]})
    assert missing.status_code == 400


async def test_targets_are_checked_against_authoritative_records(r4_http, r4_world, r4_ids):
    """员工代次不符 409；非管理员 403；另一企业的管理员看不到这个能力 404；机器身份没有「我的能力」。"""
    admin = r4_http['admin']
    cap = await _ready(admin, r4_world, r4_ids)
    stale = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice') + 5}
    assert (await _assign(admin, cap, stale)).status_code == 409
    good = {'kind': 'user', 'id': 'usr_alice', 'user_epoch': await _epoch(r4_world, 'usr_alice')}
    assert (await _assign(r4_http['bob'], cap, good, 'by-bob')).status_code == 403
    # 另一企业也开启能力交付：仍然看不到本企业的能力（404，不暴露是否存在）
    from xuanpu_server.capabilities.config import CapabilityDeliveryConfig
    r4_world.platform.capabilities.config = CapabilityDeliveryConfig(enabled=True, tenants=('acme', 'beta'))
    assert (await _assign(r4_http['outsider'], cap, good, 'by-outsider')).status_code == 404
    assert (await r4_http['machine'].get('/v1/me/capabilities')).status_code == 403


async def test_position_assignment_follows_real_membership(r4_http, r4_world, r4_ids):
    """按岗位分配：有真实任职才可用；任职撤销或岗位结构变化后失效（需重新确认）。"""
    admin = r4_http['admin']
    cap = await _ready(admin, r4_world, r4_ids)
    epoch = await _epoch(r4_world, 'usr_alice')
    store = r4_world.platform.organization.store
    now = time.time()
    async with store.transaction('acme', participants=('usr_alice',)) as tx:
        repo = OrganizationRepository(tx)
        repo.put_unit(OrgUnit(tenant_id='acme', id='u-ops', name='运营', created_at=now, updated_at=now))
        repo.put_position(Position(tenant_id='acme', id='p-clerk', unit_id='u-ops', name='专员',
                                   created_at=now, updated_at=now))
        repo.put_membership(Membership(tenant_id='acme', id='m-alice', user_id='usr_alice', user_epoch=epoch,
                                       unit_id='u-ops', position_id='p-clerk', valid_from=now - 1,
                                       created_at=now, updated_at=now))
    assigned = await _assign(admin, cap, {'kind': 'position', 'id': 'p-clerk', 'target_version': 1})
    assert assigned.status_code == 201, assigned.text
    assert await _mine(r4_http['alice']) == [cap]
    assert await _mine(r4_http['bob']) == []
    async with store.transaction('acme', participants=('usr_alice',)) as tx:
        member = tx.organization.memberships['m-alice']
        OrganizationRepository(tx).put_membership(member.model_copy(update={
            'status': 'revoked', 'version': member.version + 1, 'updated_at': time.time()}))
    assert await _mine(r4_http['alice']) == []
    renamed = await _assign(admin, cap, {'kind': 'position', 'id': 'p-clerk', 'target_version': 2}, 'v2')
    assert renamed.status_code == 409  # 岗位仍是版本 1：客户端自报的版本不被采信

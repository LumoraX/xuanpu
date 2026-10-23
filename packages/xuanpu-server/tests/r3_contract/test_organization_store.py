"""组织实体复用协调事务，约束失败不产生部分写入。"""
from __future__ import annotations

import pytest


async def test_org_cycle_and_duplicate_roll_back(r3_world, r3_http):
    """存储约束不允许同级重名和父子循环。"""
    assert hasattr(r3_world.platform, 'coordination'), '缺少组织协调事务'
    from xuanpu_server.errors import ConflictError
    from xuanpu_server.organization.models import OrgUnit
    from xuanpu_server.organization.store import OrganizationRepository

    store = r3_world.platform.coordination
    async with store.transaction('acme') as tx:
        repo = OrganizationRepository(tx)
        repo.put_unit(OrgUnit(tenant_id='acme', id='root', name='研发', created_at=1, updated_at=1))
        repo.put_unit(OrgUnit(tenant_id='acme', id='child', name='一组', parent_id='root', created_at=1, updated_at=1))
    for unit in (
        OrgUnit(tenant_id='acme', id='root', name='研发', parent_id='child', version=2, created_at=1, updated_at=2),
        OrgUnit(tenant_id='acme', id='duplicate', name='研发', created_at=1, updated_at=1),
    ):
        with pytest.raises(ConflictError):
            async with store.transaction('acme') as tx:
                OrganizationRepository(tx).put_unit(unit)
    async with store.transaction('acme') as tx:
        assert tx.organization.units['root'].parent_id is None
        assert set(tx.organization.units) == {'root', 'child'}


async def test_org_cross_tenant_reference_and_position_mismatch(r3_world, r3_http):
    """员工、部门与岗位外键都受租户和所属部门约束。"""
    assert hasattr(r3_world.platform, 'coordination'), '缺少组织协调事务'
    from xuanpu_server.errors import ConflictError, NotFoundError
    from xuanpu_server.organization.models import Membership, OrgUnit, Position
    from xuanpu_server.organization.store import OrganizationRepository

    store = r3_world.platform.coordination
    async with store.transaction('acme') as tx:
        repo = OrganizationRepository(tx)
        for key in ('a', 'b'):
            repo.put_unit(OrgUnit(tenant_id='acme', id=key, name=key, created_at=1, updated_at=1))
        repo.put_position(Position(tenant_id='acme', id='pos-a', name='工程师', unit_id='a', created_at=1, updated_at=1))
    async with store.transaction('acme', participants=('usr_alice',)) as tx:
        repo = OrganizationRepository(tx)
        with pytest.raises(NotFoundError):
            repo.put_unit(OrgUnit(tenant_id='beta', id='foreign', name='跨租户', created_at=1, updated_at=1))
        with pytest.raises(ConflictError):
            repo.put_membership(Membership(tenant_id='acme', id='m1', user_id='usr_alice', unit_id='b', position_id='pos-a', valid_from=1, created_at=1, updated_at=1))


async def test_domain_receipt_event_are_one_commit(r3_world, r3_http):
    """异常同时回滚实体、回执和事件；重开平台仓储也不产生幽灵记录。"""
    assert hasattr(r3_world.platform, 'coordination'), '缺少组织协调事务'
    from xuanpu_server.organization.models import OrgUnit
    from xuanpu_server.organization.store import OrganizationRepository

    store = r3_world.platform.coordination
    with pytest.raises(ValueError, match='拒绝提交'):
        async with store.transaction('acme') as tx:
            OrganizationRepository(tx).put_unit(OrgUnit(tenant_id='acme', id='lost', name='不提交', created_at=1, updated_at=1))
            tx.record_receipt('usr_alice', 'atomic', {}, {'id': 'lost'})
            tx.emit('membership_changed', 'lost', 1, actor='usr_alice', at=1)
            raise ValueError('拒绝提交')
    async with store.transaction('acme') as tx:
        assert not tx.organization.units
        assert not tx.pending_events('org')
        assert tx.receipt('usr_alice', 'atomic', {}) is None


async def test_duplicate_active_role_is_rejected(r3_world, r3_http):
    """同一员工、职责、范围只能有一条启用授予，撤销不会留下隐藏副本。"""
    from xuanpu_server.errors import ConflictError
    from xuanpu_server.organization.models import RoleAssignment
    from xuanpu_server.organization.store import OrganizationRepository

    async with r3_world.platform.coordination.transaction('acme', participants=('usr_alice',)) as tx:
        repo = OrganizationRepository(tx)
        for key in ('first', 'duplicate'):
            role = RoleAssignment(tenant_id='acme', id=key, user_id='usr_alice',
                                  role='org_manager', created_at=1, updated_at=1)
            if key == 'first':
                repo.put_role(role)
            else:
                with pytest.raises(ConflictError):
                    repo.put_role(role)

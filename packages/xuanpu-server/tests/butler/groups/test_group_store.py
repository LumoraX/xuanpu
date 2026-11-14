"""编组存储契约：隔离、幂等、版本、进行中列表。memory 与 PostgreSQL 各跑一遍。"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg
import pytest
from taifeng_stores import SqlNamespace
from xuanpu_server.butler.config import ButlerConfig
from xuanpu_server.butler.groups.memory import MemoryGroupStore
from xuanpu_server.butler.groups.plan import GroupRecord, group_fingerprint
from xuanpu_server.butler.groups.postgres import PostgresGroupStore
from xuanpu_server.butler.groups.validate import parse_plan
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.controlplane.schema import apply_migrations
from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.tenancy import Tenant
from xuanpu_server.tracing.models import TraceOwner

from .helpers import member, plan

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.butler.groups.store import GroupStore

T0 = datetime(2026, 11, 14, 8, tzinfo=UTC)
ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
BOB = TraceOwner(tenant_id="acme", user_id="usr_bob")
PLAN = parse_plan(plan({"mode": "parallel", "members": [member("A")]}), ButlerConfig())


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[GroupStore]:
    """同一套用例对两个实现各跑一遍。"""
    if request.param == "memory":
        yield MemoryGroupStore()
        return
    dsn = request.getfixturevalue("postgres_dsn")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    namespace = SqlNamespace(schema="t_" + secrets.token_hex(6), prefix="xp_")
    try:
        await apply_migrations(pool, namespace)
        await PostgresControlPlane(pool, namespace=namespace).create_tenant(
            Tenant(tenant_id="acme", name="acme"))
        users = PostgresIdentityStore(pool, namespace=namespace)
        for owner in (ALICE, BOB):
            await users.create_user(UserRecord(tenant_id="acme", user_id=owner.user_id,
                                               display_name=owner.user_id, created_at=1.0))
        yield PostgresGroupStore(pool, namespace=namespace)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace.schema}" CASCADE')
        await pool.close()


def _record(group_id: str = "grp_1", title: str | None = None) -> GroupRecord:
    """Alice 的一个编组。"""
    group_plan = PLAN if title is None else PLAN.model_copy(update={"title": title})
    return GroupRecord(group_id=group_id, tenant_id="acme", user_id="usr_alice", request_id="r1",
                       input_fingerprint=group_fingerprint("trc_1", group_plan), trace_id="trc_1",
                       plan=group_plan, created_at=T0, updated_at=T0)


async def test_idempotent_isolated_versioned(store: GroupStore) -> None:
    """同键同体回放、异体冲突；他人 404；保存须带期望版本；收口后不在进行中列表。"""
    created, fresh = await store.create(ALICE, _record())
    assert fresh
    again, fresh = await store.create(ALICE, _record("grp_other"))
    assert not fresh and again == created
    with pytest.raises(ConflictError, match="内容不同"):
        await store.create(ALICE, _record("grp_x", title="别的"))
    with pytest.raises(NotFoundError):
        await store.get(BOB, "grp_1")
    assert await store.find_by_request(BOB, "r1") is None
    assert (await store.find_by_request(ALICE, "r1")) == created
    saved = await store.save(ALICE, created.model_copy(update={"step": 0, "cancel_requested": True}),
                             expected_version=1)
    assert saved.version == 2 and saved.cancel_requested
    with pytest.raises(ConflictError, match="已被更新"):
        await store.save(ALICE, created, expected_version=1)
    assert [r.group_id for r in await store.active("acme")] == ["grp_1"]
    closed = saved.model_copy(update={"state": "completed", "summarized": True})
    await store.save(ALICE, closed, expected_version=2)
    assert await store.active("acme") == ()

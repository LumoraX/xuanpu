"""派工存储契约：隔离、幂等、容量（含并发）、版本。memory 与 PostgreSQL 各跑一遍。"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg
import pytest
from taifeng_stores import SqlNamespace
from xuanpu_server.butler.budget import ButlerBudgetError
from xuanpu_server.butler.memory import MemoryDispatchStore
from xuanpu_server.butler.models import ArtisanRef, DispatchRecord, dispatch_fingerprint
from xuanpu_server.butler.postgres import PostgresDispatchStore
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.controlplane.schema import apply_migrations
from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.tenancy import Tenant
from xuanpu_server.tracing.models import TraceOwner

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.butler.dispatch_store import DispatchStore

T0 = datetime(2026, 11, 14, 8, tzinfo=UTC)
ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
BOB = TraceOwner(tenant_id="acme", user_id="usr_bob")
GLOBEX_ALICE = TraceOwner(tenant_id="globex", user_id="usr_alice")
REF = ArtisanRef(package="support-desk", version="1.0.0", digest="ab" * 32, entry="ticket",
                 decision_ref="catalog:support-desk@1.0.0")
LIMITS = {"max_per_trace": 2, "max_active_traces": 2}


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[DispatchStore]:
    """同一套用例对两个实现各跑一遍。"""
    if request.param == "memory":
        yield MemoryDispatchStore()
        return
    dsn = request.getfixturevalue("postgres_dsn")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    namespace = SqlNamespace(schema="t_" + secrets.token_hex(6), prefix="xp_")
    try:
        await apply_migrations(pool, namespace)
        control = PostgresControlPlane(pool, namespace=namespace)
        users = PostgresIdentityStore(pool, namespace=namespace)
        for tenant in ("acme", "globex"):
            await control.create_tenant(Tenant(tenant_id=tenant, name=tenant))
        for owner in (ALICE, BOB, GLOBEX_ALICE):
            await users.create_user(UserRecord(tenant_id=owner.tenant_id, user_id=owner.user_id,
                                               display_name=owner.user_id, created_at=1.0))
        yield PostgresDispatchStore(pool, namespace=namespace)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace.schema}" CASCADE')
        await pool.close()


def _record(owner: TraceOwner = ALICE, *, dispatch_id: str = "dsp_1", request_id: str = "r1",
            trace_id: str = "trc_1", instruction: str = "查工单") -> DispatchRecord:
    """构造一条派工。"""
    return DispatchRecord(
        dispatch_id=dispatch_id, tenant_id=owner.tenant_id, user_id=owner.user_id,
        request_id=request_id, trace_id=trace_id, artisan=REF, host="session",
        instruction=instruction, created_at=T0, updated_at=T0,
        input_fingerprint=dispatch_fingerprint(trace_id=trace_id, artisan=REF,
                                               instruction=instruction, materials=()))


async def test_create_is_idempotent_and_isolated(store: DispatchStore) -> None:
    """同键同体回原记录；同键异体冲突；他人、他租户读取 404；归属不符拒绝。"""
    created, fresh = await store.create(ALICE, _record(), **LIMITS)
    assert fresh
    again, fresh = await store.create(ALICE, _record(dispatch_id="dsp_other"), **LIMITS)
    assert not fresh and again == created
    with pytest.raises(ConflictError, match="内容不同"):
        await store.create(ALICE, _record(dispatch_id="dsp_x", instruction="别的"), **LIMITS)
    with pytest.raises(ConflictError, match="归属"):
        await store.create(BOB, _record(), **LIMITS)
    for owner in (BOB, GLOBEX_ALICE):
        with pytest.raises(NotFoundError):
            await store.get(owner, "dsp_1")
        assert await store.list_for_trace(owner, "trc_1") == ()
    assert await store.get(ALICE, "dsp_1") == created


async def test_capacity_per_trace_and_tenant(store: DispatchStore) -> None:
    """每 trace 匠人数与租户并发 trace；超限不建记录；结束的派工让出并发名额。"""
    await store.create(ALICE, _record(), **LIMITS)
    await store.create(ALICE, _record(dispatch_id="dsp_2", request_id="r2"), **LIMITS)
    with pytest.raises(ButlerBudgetError) as caught:
        await store.create(ALICE, _record(dispatch_id="dsp_3", request_id="r3"), **LIMITS)
    assert caught.value.reason == "artisans_per_trace"
    assert len(await store.list_for_trace(ALICE, "trc_1")) == 2
    await store.create(BOB, _record(BOB, dispatch_id="dsp_b", trace_id="trc_b"), **LIMITS)
    assert await store.active_traces("acme") == 2
    with pytest.raises(ButlerBudgetError) as caught:
        await store.create(ALICE, _record(dispatch_id="dsp_4", request_id="r4", trace_id="trc_2"),
                           **LIMITS)
    assert caught.value.reason == "active_traces_per_tenant"
    assert await store.active_traces("globex") == 0
    await store.update(BOB, "dsp_b", expected_version=1, to="cancelled", at=T0)
    await store.create(ALICE, _record(dispatch_id="dsp_4", request_id="r4", trace_id="trc_2"),
                       **LIMITS)


async def test_concurrent_creates_respect_trace_limit(store: DispatchStore) -> None:
    """并发派工不能一起越过上限。"""
    results = await asyncio.gather(*(
        store.create(ALICE, _record(dispatch_id=f"dsp_{i}", request_id=f"r{i}"), **LIMITS)
        for i in range(6)), return_exceptions=True)
    assert sum(1 for r in results if isinstance(r, tuple)) == 2
    assert all(isinstance(r, ButlerBudgetError) for r in results if not isinstance(r, tuple))


async def test_update_needs_expected_version(store: DispatchStore) -> None:
    """版本不符冲突；状态机由存储执行；他人改 404。"""
    await store.create(ALICE, _record(), **LIMITS)
    running = await store.update(ALICE, "dsp_1", expected_version=1, to="running", at=T0,
                                 host_ref="ses_1")
    assert running.version == 2 and running.host_ref == "ses_1"
    with pytest.raises(ConflictError, match="已被更新"):
        await store.update(ALICE, "dsp_1", expected_version=1, to="completed", at=T0)
    with pytest.raises(ConflictError):
        await store.update(ALICE, "dsp_1", expected_version=2, to="delivered", at=T0)
    with pytest.raises(NotFoundError):
        await store.update(BOB, "dsp_1", expected_version=2, to="completed", at=T0)
    done = await store.update(ALICE, "dsp_1", expected_version=2, to="completed", at=T0,
                              end_reason="ok")
    assert (done.state, done.end_reason, done.active) == ("completed", "ok", False)
    assert await store.get(ALICE, "dsp_1") == done

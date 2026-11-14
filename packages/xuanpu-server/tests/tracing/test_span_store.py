"""span 存储契约：隔离、幂等、跳号、上限、分页、清理。memory 与 PostgreSQL 各跑一遍。"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import TYPE_CHECKING

import asyncpg
import pytest
from taifeng_stores import SqlNamespace
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.controlplane.schema import apply_migrations
from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.tenancy import Tenant
from xuanpu_server.tracing.memory import MemorySpanStore
from xuanpu_server.tracing.postgres import PostgresSpanStore

from .helpers import ALICE, BOB, OTHER_TENANT_ALICE, T0, at, fact

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.tracing.store import SpanStore


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[SpanStore]:
    """同一套用例对两个实现各跑一遍；小上限便于覆盖截断。"""
    limits = {"max_spans_per_trace": 16, "max_attempts_per_span": 4}
    if request.param == "memory":
        yield MemorySpanStore(**limits)
        return
    dsn = request.getfixturevalue("postgres_dsn")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    namespace = SqlNamespace(schema="t_" + secrets.token_hex(6), prefix="xp_")
    try:
        await apply_migrations(pool, namespace)
        control = PostgresControlPlane(pool, namespace=namespace)
        for tenant in ("acme", "globex"):
            await control.create_tenant(Tenant(tenant_id=tenant, name=tenant))
        yield PostgresSpanStore(pool, namespace=namespace, **limits)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace.schema}" CASCADE')
        await pool.close()


async def _open(store: SpanStore, trace_id: str = "trc_1", owner=ALICE, ms: int = 0):
    """以给定归属建 trace。"""
    return await store.open_trace(owner, trace_id=trace_id, origin="user", title="查工单", at=at(ms))


async def test_open_is_idempotent_and_identifier_is_owned(store: SpanStore) -> None:
    """同归属重复建返回原记录；他人用同一标识报冲突。"""
    first = await _open(store)
    assert await _open(store) == first
    with pytest.raises(ConflictError):
        await _open(store, owner=BOB)


@pytest.mark.parametrize("intruder", [BOB, OTHER_TENANT_ALICE])
async def test_other_owners_see_not_found(store: SpanStore, intruder) -> None:
    """跨员工、跨租户读写都是 404，不暴露存在性。"""
    await _open(store)
    with pytest.raises(NotFoundError):
        await store.get(intruder, "trc_1")
    with pytest.raises(NotFoundError):
        await store.apply(intruder, "trc_1", [fact(1, "sp", kind="butler", name="管家")])
    assert (await store.list(intruder)).total == 0


async def test_duplicates_skip_and_gaps_flag(store: SpanStore) -> None:
    """已应用序号跳过；跳号标不完整但仍应用后续事实。"""
    await _open(store)
    await store.apply(ALICE, "trc_1", [fact(1, "sp_b", kind="butler", name="管家", state="running")])
    record = await store.apply(ALICE, "trc_1", [fact(1, "sp_b", state="failed")])
    assert record.last_seq == 1 and not record.incomplete
    assert (await store.get(ALICE, "trc_1")).spans[0].state == "running"
    record = await store.apply(ALICE, "trc_1", [fact(3, "sp_b", 10, state="completed")])
    assert record.incomplete
    assert (await store.get(ALICE, "trc_1")).spans[0].state == "completed"


async def test_rejected_fact_flags_instead_of_raising(store: SpanStore) -> None:
    """不认识的 span 缺类型：不抛给采集器，只标不完整。"""
    await _open(store)
    record = await store.apply(ALICE, "trc_1", [fact(1, "sp_ghost", state="running")])
    assert record.incomplete
    assert (await store.get(ALICE, "trc_1")).spans == ()


async def test_span_cap_truncates(store: SpanStore) -> None:
    """超过单条 trace 的 span 上限停止新增并标截断。"""
    await _open(store)
    facts = [fact(i, f"sp_{i}", i, kind="tool", name="grep", parent_span_id="sp_1")
             for i in range(1, 21)]
    record = await store.apply(ALICE, "trc_1", facts)
    assert record.truncated and record.incomplete
    assert len((await store.get(ALICE, "trc_1")).spans) == 16


async def test_out_of_order_batch_is_sorted(store: SpanStore) -> None:
    """同一批内乱序的事实按序号应用。"""
    await _open(store)
    record = await store.apply(ALICE, "trc_1", [
        fact(2, "sp_b", 50, state="completed"),
        fact(1, "sp_b", 0, kind="butler", name="管家", state="running"),
    ])
    assert not record.incomplete
    assert (await store.get(ALICE, "trc_1")).spans[0].state == "completed"


async def test_list_pages_with_total_independent_of_page(store: SpanStore) -> None:
    """倒序分页，总数与当前页无关，游标稳定。"""
    for i in range(5):
        await _open(store, trace_id=f"trc_{i}", ms=i * 1000)
    await _open(store, trace_id="trc_bob", owner=BOB)
    first = await store.list(ALICE, limit=2)
    assert [t.trace_id for t in first.items] == ["trc_4", "trc_3"]
    assert first.total == 5 and first.next_cursor is not None
    second = await store.list(ALICE, limit=2, cursor=first.next_cursor)
    assert [t.trace_id for t in second.items] == ["trc_2", "trc_1"]
    third = await store.list(ALICE, limit=2, cursor=second.next_cursor)
    assert [t.trace_id for t in third.items] == ["trc_0"] and third.next_cursor is None


async def test_list_filters_active(store: SpanStore) -> None:
    """进行中与已结束分开列；总数按过滤后计算。"""
    await _open(store, "trc_run")
    await _open(store, "trc_done", ms=1)
    await store.apply(ALICE, "trc_run", [fact(1, "sp", kind="butler", name="管家", state="running")])
    await store.apply(ALICE, "trc_done", [fact(1, "sp", kind="butler", name="管家", state="completed")])
    active = await store.list(ALICE, active=True)
    assert [t.trace_id for t in active.items] == ["trc_run"] and active.total == 1


@pytest.mark.parametrize("limit", [0, 101])
async def test_bad_page_size_and_cursor(store: SpanStore, limit: int) -> None:
    """分页参数越界或游标损坏报请求错误，不悄悄从头开始。"""
    with pytest.raises(InvalidRequestError):
        await store.list(ALICE, limit=limit)
    with pytest.raises(InvalidRequestError):
        await store.list(ALICE, cursor="!!not-a-cursor!!")


async def test_mark_incomplete(store: SpanStore) -> None:
    """采集失败时显式标记。"""
    await _open(store)
    assert (await store.mark_incomplete(ALICE, "trc_1")).incomplete


async def test_purge_is_per_tenant_and_batched(store: SpanStore) -> None:
    """只删本租户、早于期限的；按批。"""
    for i in range(3):
        await _open(store, trace_id=f"trc_old_{i}", ms=i)
    await _open(store, trace_id="trc_new", ms=10_000_000)
    await _open(store, trace_id="trc_globex", owner=OTHER_TENANT_ALICE)
    cutoff = T0 + timedelta(hours=1)
    assert await store.purge("acme", before=cutoff, batch=2) == 2
    assert await store.purge("acme", before=cutoff, batch=2) == 1
    assert (await store.list(ALICE)).total == 1
    assert (await store.list(OTHER_TENANT_ALICE)).total == 1

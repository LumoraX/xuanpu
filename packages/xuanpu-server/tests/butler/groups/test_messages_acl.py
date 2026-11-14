"""会诊消息区：只追加、按键去重、序号连续、按员工隔离（memory 与 PostgreSQL 各跑一遍）；
接口只给本人，他人 404、机器身份 403。"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
from taifeng_stores import SqlNamespace
from xuanpu_server.butler.groups.message_postgres import PostgresMessageStore
from xuanpu_server.butler.groups.messages import GroupMessage, MemoryMessageStore
from xuanpu_server.butler.groups.plan import GroupPlan, GroupRecord
from xuanpu_server.butler.groups.postgres import PostgresGroupStore
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.controlplane.schema import apply_migrations
from xuanpu_server.errors import NotFoundError
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.tenancy import Tenant
from xuanpu_server.tracing.models import TraceOwner

from ..conftest import employee

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_server.butler.groups.messages import MessageStore

T0 = datetime(2026, 11, 14, 8, tzinfo=UTC)
ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
BOB = TraceOwner(tenant_id="acme", user_id="usr_bob")


def _group() -> GroupRecord:
    """Alice 的一个会诊编组。"""
    plan = GroupPlan.model_validate({"title": "会诊", "steps": [{"mode": "consult", "members": [
        {"package": "support-desk", "entry": "triage", "brief": "怎么办"}]}]})
    return GroupRecord(group_id="grp_1", tenant_id="acme", user_id="usr_alice", request_id="r",
                       input_fingerprint="0" * 64, trace_id="trc_1", plan=plan,
                       created_at=T0, updated_at=T0)


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[MessageStore]:
    """同一套用例对两个实现各跑一遍。"""
    if request.param == "memory":
        yield MemoryMessageStore()
        return
    dsn = request.getfixturevalue("postgres_dsn")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
    namespace = SqlNamespace(schema="t_" + secrets.token_hex(6), prefix="xp_")
    try:
        await apply_migrations(pool, namespace)
        await PostgresControlPlane(pool, namespace=namespace).create_tenant(
            Tenant(tenant_id="acme", name="acme"))
        users = PostgresIdentityStore(pool, namespace=namespace)
        for owner in (ALICE, BOB):
            await users.create_user(UserRecord(tenant_id="acme", user_id=owner.user_id,
                                               display_name=owner.user_id, created_at=1.0))
        await PostgresGroupStore(pool, namespace=namespace).create(ALICE, _group())
        yield PostgresMessageStore(pool, namespace=namespace)
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace.schema}" CASCADE')
        await pool.close()


def _message(key: str, owner: TraceOwner = ALICE, text: str = "结论") -> GroupMessage:
    """一条成员消息。"""
    return GroupMessage(group_id="grp_1", tenant_id=owner.tenant_id, user_id=owner.user_id,
                        key=key, author="member", step=0, member=0, text=text, created_at=T0)


async def test_append_only_dedup_and_isolation(store: MessageStore) -> None:
    """同键回放原消息（内容不变）；序号连续；他人看不到、不能以他人身份写。"""
    first, fresh = await store.append(ALICE, _message("pub:a"))
    assert fresh and first.seq == 1
    again, fresh = await store.append(ALICE, _message("pub:a", text="改过的内容"))
    assert not fresh and again == first
    second, _ = await store.append(ALICE, _message("pub:b"))
    assert second.seq == 2
    assert [m.key for m in await store.list(ALICE, "grp_1")] == ["pub:a", "pub:b"]
    assert await store.list(BOB, "grp_1") == ()
    with pytest.raises(NotFoundError):
        await store.append(BOB, _message("pub:c"))


async def test_concurrent_appends_get_distinct_sequences(store: MessageStore) -> None:
    """并发追加：序号不重复、连续。"""
    await asyncio.gather(*(store.append(ALICE, _message(f"pub:{i}")) for i in range(8)))
    assert [m.seq for m in await store.list(ALICE, "grp_1")] == list(range(1, 9))


async def test_api_only_owner(butler: dict[str, Any]) -> None:
    """本人能读编组与消息区；同租户他人 404；机器身份 403；不存在 404。"""
    world = butler["world"]
    record = _group()
    await world.platform.store.group_store.create(ALICE, record)
    await world.platform.store.group_message_store.append(ALICE, _message("pub:x", text="甲"))
    http = butler["http"]
    detail = (await http.get("/v1/assistant/groups/grp_1")).json()
    assert detail["state"] == "running" and detail["steps"][0]["mode"] == "consult"
    assert "tenant_id" not in detail and "user_id" not in detail
    items = (await http.get("/v1/assistant/groups/grp_1/messages")).json()["items"]
    assert [(i["member"], i["text"]) for i in items] == [(1, "甲")]
    butler["as"]["who"] = await employee(world, "usr_bob")
    for path in ("/v1/assistant/groups/grp_1", "/v1/assistant/groups/grp_1/messages"):
        assert (await http.get(path)).status_code == 404
    butler["as"]["who"] = world.member
    assert (await http.get("/v1/assistant/groups/grp_1/messages")).status_code == 403
    butler["as"]["who"] = butler["alice"]
    assert (await http.get("/v1/assistant/groups/nope/messages")).status_code == 404

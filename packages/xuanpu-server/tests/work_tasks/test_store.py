"""内存和真实PostgreSQL上相同的持久受理、权限和CAS契约。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.work_tasks.store import WorkTaskStore


async def test_create_is_atomic_and_replays_once(task_env):
    """并发重复请求只创建一个任务和一条accepted事件。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    results = await asyncio.gather(*(store.create_or_replay(env.who, env.record) for _ in range(4)))
    assert sum(created for _, created in results) == 1
    assert {task.task_id for task, _ in results} == {"task1"}
    assert len(await store.events(env.who, "task1")) == 1
    assert len(await store.list(env.who)) == 1
    conflict = env.record.model_copy(update={"task_id": "task2", "input_fingerprint": "c" * 64})
    with pytest.raises(ConflictError):
        await store.create_or_replay(env.who, conflict)
    assert len(await store.list(env.who)) == 1


async def test_private_scope_and_disable(task_env):
    """管理员与他人没有正文通道，停用员工也不能查询。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    await store.create_or_replay(env.who, env.record)
    for stranger in (replace(env.who, user_id="usr_b"),
                     replace(env.who, user_id="usr_b", role="tenant_admin")):
        assert await store.list(stranger) == []
        with pytest.raises(NotFoundError):
            await store.get(stranger, "task1")
        with pytest.raises(NotFoundError):
            await store.events(stranger, "task1")
    with pytest.raises(PermissionDeniedError):
        await store.get(replace(env.who, user_id=None, key_id="machine"), "task1")
    await env.identity.set_user_status("acme", "usr_a", "disabled", reason="admin", at=1001)
    with pytest.raises(PermissionDeniedError):
        await store.get(env.who, "task1")


async def test_transition_cas_and_persistent_events(task_env):
    """同一版本的并发状态更新只提交一个事件，刷新能补读。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    await store.create_or_replay(env.who, env.record)
    results = await asyncio.gather(*(store.transition(env.who, "task1", "running",
        expected_version=1, at=1001.0) for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    refreshed = WorkTaskStore(env.workspace)
    assert (await refreshed.get(env.who, "task1")).state_version == 2
    events = await refreshed.events(env.who, "task1")
    assert [event.status for event in events] == ["accepted", "running"]
    assert await refreshed.events(env.who, "task1", after_id=events[0].event_id) == events[1:]


async def test_source_validation_and_transaction_rollback(task_env):
    """受理与文件版本复核在同一员工事务；异常不能留下半任务。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    await env.workspace.trash(env.who, env.file["id"], 1)
    with pytest.raises((ConflictError, NotFoundError)):
        await store.create_or_replay(env.who, env.record)
    assert await store.list(env.who) == []
    await env.workspace.restore(env.who, env.file["id"], 2)
    async with env.workspace.transaction(env.who, "work-task:test") as state:
        assert state.work_tasks == {}
    with pytest.raises(RuntimeError):
        async with env.workspace.transaction(env.who, "work-task:test") as state:
            state.work_tasks["task1"] = env.record.model_dump(mode="json")
            raise RuntimeError("injected rollback")
    assert await store.list(env.who) == []


async def test_cross_tenant_scope_and_forged_record(task_env):
    """跨租户员工即便拥有同名用户也不能读任务或冒用来源。"""
    from xuanpu_server.identity.models import UserRecord
    from xuanpu_server.tenancy import Tenant

    env = task_env
    store = WorkTaskStore(env.workspace)
    await store.create_or_replay(env.who, env.record)
    await env.control.create_tenant(Tenant(tenant_id="other", name="另一公司", created_at=1.0))
    await env.identity.create_user(UserRecord(tenant_id="other", user_id="usr_a", display_name="A", created_at=1.0))
    stranger = replace(env.who, tenant_id="other")
    with pytest.raises(NotFoundError):
        await store.get(stranger, "task1")
    with pytest.raises(InvalidRequestError):
        await store.create_or_replay(stranger, env.record)


async def test_accepted_event_failure_rolls_back_everything(task_env, monkeypatch):
    """数据库在任务插入后拒绝事件时，任务和输入引用必须一并回滚。"""
    import asyncpg
    from xuanpu_server.controlplane.postgres import PostgresControlPlane

    env = task_env
    if not isinstance(env.control, PostgresControlPlane):
        def fail_audit(_):
            """注入内存事务提交失败。"""
            raise RuntimeError("injected event failure")

        with monkeypatch.context() as patch:
            patch.setattr(env.workspace.store, "_audit", fail_audit)
            with pytest.raises(RuntimeError, match="injected event failure"):
                await WorkTaskStore(env.workspace).create_or_replay(env.who, env.record)
        assert await WorkTaskStore(env.workspace).list(env.who) == []
        return
    pool, namespace = env.control._pool, env.control._namespace
    event_table = namespace.table("work_task_event")
    function = namespace.table("fail_work_task_event")
    async with pool.acquire() as conn:
        await conn.execute(f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected event failure'; END $$")
        await conn.execute(f"CREATE TRIGGER fail_event BEFORE INSERT ON {event_table} FOR EACH ROW EXECUTE FUNCTION {function}()")
    store = WorkTaskStore(env.workspace)
    with pytest.raises(asyncpg.RaiseError, match="injected event failure"):
        await store.create_or_replay(env.who, env.record)
    for name in ("work_task", "work_task_input", "work_task_event"):
        async with pool.acquire() as conn:
            assert await conn.fetchval(f"SELECT count(*) FROM {namespace.table(name)}") == 0
    assert await store.list(env.who) == []

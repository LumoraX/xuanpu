"""持久派发的双后端原子受理、执行所有权及阶段回归。"""
from __future__ import annotations

import asyncio

import pytest
from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.work_tasks.execution_store import ExecutionStore
from xuanpu_server.work_tasks.store import WorkTaskStore


async def prepared(env):
    """创建R2任务，派发和授权必须与任务同一次事务保存。"""
    task = env.record.model_copy(update={'recovery_policy_version': 2})
    tasks = WorkTaskStore(env.workspace)
    task, _ = await tasks.create_or_replay(env.who, task)
    return tasks, ExecutionStore(env.workspace), task


async def test_accept_replay_has_one_dispatch_and_grant(task_env):
    """重复受理不复制执行授权或派发记录。"""
    tasks, execution, task = await prepared(task_env)
    replay, created = await tasks.create_or_replay(task_env.who, task)
    assert not created and replay == task
    dispatch = await execution.get(task_env.who, task.task_id)
    assert dispatch.state == 'ready' and dispatch.attempt_no == 0
    assert task.grant_id
    assert (await execution.grant(task_env.who, task.grant_id)).max_attempts == 3


async def test_concurrent_claim_single_attempt(task_env):
    """并发认领只产生一个有效租约和一次执行。"""
    _, execution, task = await prepared(task_env)
    results = await asyncio.gather(*(execution.claim(task_env.who, task.task_id, owner='p', now=1000.) for _ in range(2)))
    assert sum(result is not None for result in results) == 1
    lease = next(result for result in results if result is not None)
    assert lease.attempt_no == 1 and lease.epoch == 1


async def test_expired_owner_cannot_checkpoint_or_renew(task_env):
    """租约一旦过期不能续活，且旧结果无法提交。"""
    tasks, execution, task = await prepared(task_env)
    old = await execution.claim(task_env.who, task.task_id, owner='old', now=1000.)
    with pytest.raises(ConflictError):
        await execution.renew(old, now=1061.)
    with pytest.raises(ConflictError):
        await execution.checkpoint(old, 'executing', now=1061.)
    assert await execution.claim(task_env.who, task.task_id, owner='new', now=1061.) is None
    await tasks.resources_closed(await tasks.get(task_env.who, task.task_id), at=1061.)
    new = await execution.claim(task_env.who, task.task_id, owner='new', now=1061.)
    assert new.epoch > old.epoch and new.attempt_no == 2
    with pytest.raises(ConflictError):
        await execution.checkpoint(old, 'executing', now=1061.)


async def test_claim_does_not_cross_employee_or_cancel(task_env):
    """身份和取消先于认领；管理权限不扩大内容范围。"""
    from dataclasses import replace
    tasks, execution, task = await prepared(task_env)
    with pytest.raises(NotFoundError):
        await execution.get(replace(task_env.who, user_id='usr_b'), task.task_id)
    await tasks.transition(task_env.who, task.task_id, 'cancelled', expected_version=1, at=1000.)
    assert await execution.claim(task_env.who, task.task_id, owner='p', now=1000.) is None

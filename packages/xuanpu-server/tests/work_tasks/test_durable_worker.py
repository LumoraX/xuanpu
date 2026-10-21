"""同一Registry的持久派发、停机责任与有限重试。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from xuanpu_server.work_tasks.config import DurableExecutionConfig
from xuanpu_server.work_tasks.registry import TaskRegistry

from .test_execution_store import prepared


async def test_shutdown_keeps_business_nonterminal_and_recovery_responsibility(task_env):
    """正常停机只暂停派发，资源真正退出后新owner才能再次执行。"""
    env = task_env
    tasks, execution, task = await prepared(env)
    entered = asyncio.Event()
    async def run(who, current, decision=None):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await tasks.resources_closed(current, at=1001.)
    registry = TaskRegistry(tasks, SimpleNamespace(run=run), process_owner='p1', now=lambda:1001.,
                            durable=DurableExecutionConfig(enabled=True, verified_text_bounds=True))
    await registry.open(scan=False)
    registry.start(env.who, task)
    await asyncio.wait_for(entered.wait(), 5)
    await registry.close()
    current = await tasks.get(env.who, task.task_id)
    assert current.status == 'running' and not current.resources_pending
    assert (await execution.get(env.who, task.task_id)).state == 'paused_shutdown'
    resumed = []
    async def next_run(who, current, decision=None):
        resumed.append(current.attempt_no)
        await tasks.resources_closed(current, at=1002.)
        return {'status':'waiting_user'}
    new = TaskRegistry(tasks, SimpleNamespace(run=next_run), process_owner='p2', now=lambda:1002.,
                       durable=DurableExecutionConfig(enabled=True, verified_text_bounds=True))
    try:
        await new.open(scan=False)
        await new.reconcile()
        await new.join()
        assert resumed == [2]
    finally:
        await new.close()


async def test_unknown_resource_creation_remains_blocked(task_env):
    """创建回执丢失没有资源退出证明时，不允许下一attempt。"""
    tasks, execution, task = await prepared(task_env)
    async def run(*args):
        raise OSError('创建结果未知')
    registry = TaskRegistry(tasks, SimpleNamespace(run=run), process_owner='p1', now=lambda:1001.,
                            durable=DurableExecutionConfig(enabled=True))
    try:
        await registry.open(scan=False)
        registry.start(task_env.who, task)
        await registry.join()
        dispatch = await execution.get(task_env.who, task.task_id)
        current = await tasks.get(task_env.who, task.task_id)
        assert dispatch.state == 'blocked' and dispatch.block_reason == 'cleanup_unverified'
        assert current.resources_pending and current.attempt_no == 1
    finally:
        await registry.close()


async def test_smaller_attempt_limit_applies_after_hard_loss(task_env):
    """不经过异常处理器的硬退出也不能绕过服务端次数上限。"""
    tasks, execution, task = await prepared(task_env)
    await execution.claim(task_env.who, task.task_id, owner='old', now=1000.)
    current = await tasks.get(task_env.who, task.task_id)
    await tasks.resources_closed(current, at=1061.)
    execution.max_attempts = 1
    assert await execution.claim(task_env.who, task.task_id, owner='new', now=1061.) is None
    assert (await execution.get(task_env.who, task.task_id)).block_reason == 'attempts_exhausted'


@pytest.mark.parametrize('callback', ['cancelled','failed'])
async def test_old_callback_never_settles_new_epoch(task_env, callback):
    """旧取消/失败回调不能借最新记录的身份终结新attempt。"""
    from xuanpu_server.errors import ConflictError

    tasks, execution, task = await prepared(task_env)
    old = await execution.claim(task_env.who,task.task_id,owner='old',now=1000.)
    original = await tasks.get(task_env.who,task.task_id)
    original = await tasks.transition(task_env.who,task.task_id,'running',expected_version=original.state_version,at=1000.)
    await tasks.resources_closed(original,at=1061.)
    new = await execution.claim(task_env.who,task.task_id,owner='new',now=1061.)
    current = await tasks.get(task_env.who,task.task_id)
    await tasks.resources_closed(current,at=1061.)
    registry = TaskRegistry(tasks,SimpleNamespace(),process_owner='old',now=lambda:1061.,
                            durable=DurableExecutionConfig(enabled=True,max_attempts=1))
    with pytest.raises(ConflictError):
        if callback == 'cancelled':
            await registry.durable._cancelled(original,old,None)
        else:
            await registry.durable._failed(original,old,OSError('旧请求迟到失败'))
    latest = await tasks.get(task_env.who,task.task_id)
    assert latest.status == 'running' and latest.process_owner == new.owner and latest.attempt_no == new.attempt_no


async def test_restart_settles_committed_cancel_intent(task_env):
    """取消意图已提交、执行取消尚未送达时重建，仍须完成取消。"""
    tasks, execution, task = await prepared(task_env)
    await execution.claim(task_env.who,task.task_id,owner='old',now=1000.)
    current = await tasks.get(task_env.who,task.task_id)
    await tasks.transition(task_env.who,task.task_id,'running',expected_version=current.state_version,
                           at=1001.,cancel_requested=True)
    async def forbidden(*args):
        raise AssertionError('取消任务不可再次执行')
    async def cleanup(record):
        await tasks.resources_closed(record,at=1002.)
    registry = TaskRegistry(tasks,SimpleNamespace(run=forbidden),process_owner='new',now=lambda:1002.,
        recover=cleanup,durable=DurableExecutionConfig(enabled=True))
    try:
        await registry.open(scan=False)
        assert (await tasks.get(task_env.who,task.task_id)).status == 'cancelled'
        assert (await execution.get(task_env.who,task.task_id)).state == 'settled'
    finally:
        await registry.close()

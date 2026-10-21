"""旧记录继续R1策略，新增授权和派发不可半提交。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from xuanpu_server.work_tasks.config import DurableExecutionConfig
from xuanpu_server.work_tasks.models import WorkTaskRecord
from xuanpu_server.work_tasks.registry import TaskRegistry
from xuanpu_server.work_tasks.store import WorkTaskStore


async def test_accept_failure_rolls_back_task_grant_dispatch_event(task_env,monkeypatch):
    """真正员工事务中断，双后端都不保留半份受理记录。"""
    workspace = task_env.workspace
    original = workspace.transaction
    @asynccontextmanager
    async def fail(who,request_id=None):
        async with original(who,request_id) as state:
            yield state
            assert state.work_tasks and state.task_grants and state.task_dispatch and state.work_task_events
            raise OSError('提交前故障')
    tasks = WorkTaskStore(workspace)
    monkeypatch.setattr(workspace,'transaction',fail)
    with pytest.raises(OSError):
        await tasks.create_or_replay(task_env.who,task_env.record.model_copy(update={'recovery_policy_version':2}))
    monkeypatch.setattr(workspace,'transaction',original)
    async with workspace.transaction(task_env.who,'work-task:accept') as state:
        assert not state.work_tasks and not state.task_grants and not state.task_dispatch and not state.work_task_events


async def test_legacy_record_terminal_never_restarts_when_r2_enabled(task_env):
    """旧JSON没有新增字段时仍默认R1，打开新开关也不重放旧失败。"""
    value = task_env.record.model_dump(mode='json')
    for key in ('recovery_policy_version','grant_id','execution_kind','entry','completion_policy'):
        value.pop(key)
    legacy = WorkTaskRecord.model_validate_json(__import__('json').dumps(value))
    assert legacy.recovery_policy_version == 1
    tasks = WorkTaskStore(task_env.workspace)
    await tasks.create_or_replay(task_env.who,legacy)
    await tasks.transition(task_env.who,legacy.task_id,'failed',expected_version=1,at=1001.)
    async def forbidden(*args):
        raise AssertionError('旧终态绝不重放')
    registry = TaskRegistry(tasks,SimpleNamespace(run=forbidden),process_owner='new',now=lambda:1002.,
        durable=DurableExecutionConfig(enabled=True))
    try:
        await registry.open(scan=False)
        await registry.reconcile()
        assert (await tasks.get(task_env.who,legacy.task_id)).status == 'failed'
        assert not registry._active
    finally:
        await registry.close()

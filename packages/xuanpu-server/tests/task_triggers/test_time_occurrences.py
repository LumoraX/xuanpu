"""一次时刻只能原子产生一个任务，资格不足不推进游标。"""
from __future__ import annotations

import asyncio

import pytest

from .conftest import request_for


async def configured(w,repeat='once'):
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    record=await service.create(w.who,await request_for(w,kind=repeat))
    return service,record


async def test_concurrent_time_ticks_create_one_task_and_one_occurrence(trigger_world):
    """工作实例、dispatch与时间游标同事务，重复扫描不重发。"""
    w=trigger_world
    service,record=await configured(w)
    assert hasattr(w.platform,'trigger_worker'),'尚无唯一触发扫描器'
    w.now+=60
    await asyncio.gather(*(w.platform.trigger_worker.tick_time() for _ in range(4)))
    async with w.platform.workspace.transaction(w.who,'work-task:trigger-inspect') as state:
        assert len(state.work_tasks)==len(state.task_occurrences)==len(state.task_dispatch)==1
        task=next(iter(state.work_tasks.values()))
        assert task['grant_id']==record.grant_id and task['definition_id']==record.definition_id
        assert state.task_definitions[record.definition_id]['next_fire_at'] is None


async def test_task_quota_blocks_same_occurrence_then_recovers(trigger_world):
    """资格阻塞提交可观察记录，原时刻保持，恢复后沿用同一稳定键。"""
    w=trigger_world
    service,record=await configured(w)
    assert hasattr(w.platform,'trigger_worker'),'尚无触发资格检查'
    service.tasks.store.max_tasks=0
    w.now+=60
    await w.platform.trigger_worker.tick_time()
    async with w.platform.workspace.transaction(w.who,'work-task:trigger-inspect') as state:
        blocked=next(iter(state.task_occurrences.values()))
        assert blocked['status']=='blocked' and not state.work_tasks
        key=blocked['occurrence_key']
        assert state.task_definitions[record.definition_id]['next_fire_at']==record.next_fire_at
    service.tasks.store.max_tasks=1000
    w.now+=60
    await w.platform.trigger_worker.tick_time()
    async with w.platform.workspace.transaction(w.who,'work-task:trigger-inspect') as state:
        assert len(state.work_tasks)==1 and len(state.task_occurrences)==1
        assert next(iter(state.task_occurrences.values()))['occurrence_key']==key


async def test_failed_commit_rolls_back_cursor_task_and_occurrence(trigger_world,monkeypatch):
    """真实事务提交阶段失败，不以推进游标假装已受理。"""
    w=trigger_world
    service,record=await configured(w)
    assert hasattr(w.platform,'trigger_worker'),'尚无触发受理事务'
    w.now+=60
    backend=w.platform.workspace.store
    if hasattr(backend,'_save'):
        original=backend._save
        async def broken(*args):
            await original(*args)
            raise OSError('commit fault')
        monkeypatch.setattr(backend,'_save',broken)
    else:
        original=backend._audit
        def broken(*args):
            raise OSError('commit fault')
        monkeypatch.setattr(backend,'_audit',broken)
    with pytest.raises(OSError):await service.fire_time(w.who,record.definition_id)
    monkeypatch.undo()
    async with w.platform.workspace.transaction(w.who,'work-task:trigger-inspect') as state:
        assert not state.work_tasks and not state.task_occurrences
        assert state.task_definitions[record.definition_id]['next_fire_at']==record.next_fire_at
    await service.fire_time(w.who,record.definition_id)
    assert len(await service.occurrences(w.who,record.definition_id))==1


async def test_version_change_cancels_unstarted_and_pause_default_keeps_running(trigger_world):
    """改变确认版本后旧排队实例终结，不能被唯一执行器再次认领。"""
    w=trigger_world
    service,record=await configured(w,'daily')
    assert hasattr(w.platform,'trigger_worker'),'尚无触发受理事务'
    w.now+=60
    await service.fire_time(w.who,record.definition_id)
    request=record.confirmed.model_dump(mode='json')
    request.update(request_id='version2',expected_version=1,trigger={'kind':'time','repeat':'daily','at':'2026-10-22T22:00:00+08:00'})
    changed=await service.update(w.who,record.definition_id,request)
    occurrence=(await service.occurrences(w.who,record.definition_id))[0]
    task=await service.tasks.store.get(w.who,occurrence.task_id)
    assert changed.version==2 and task.status=='cancelled'
    assert occurrence.status=='cancelled'
    from xuanpu_server.work_tasks.execution_store import ExecutionStore
    assert await ExecutionStore(w.platform.workspace).claim(w.who,task.task_id,owner='late',now=w.now) is None

"""真实锁外读取期间修改/取消，迟到发生实例不得越过新确认版本。"""
from __future__ import annotations

import asyncio

import pytest

from .test_event_occurrences import event_definition
from .test_time_occurrences import configured


@pytest.mark.parametrize('mode',['time','event'])
@pytest.mark.parametrize('action',['update','cancel'])
async def test_management_change_during_snapshot_blocks_late_acceptance(trigger_world,monkeypatch,mode,action):
    """双后端在实际源读取后交错管理提交，再放行旧执行者进入员工事务。"""
    from xuanpu_server.task_triggers import event_service, time_service
    w=trigger_world
    if mode=='time':
        service,record=await configured(w)
        w.now+=60
        operation=lambda:service.fire_time(w.who,record.definition_id)
        module=time_service
    else:
        service,record,source=await event_definition(w)
        await w.platform.workspace.replace_content(w.who,source,b'A,15.50','text/csv',1,'race-v2')
        async with service.workspace.transaction(w.who,'work-task:trigger-inspect') as state:
            delivery=next(iter(state.task_event_deliveries))
        operation=lambda:service.deliver_event(w.who,delivery)
        module=event_service
    ready,release=asyncio.Event(),asyncio.Event()
    original=module.snapshots
    async def hold(*args):
        """只暂停锁外快照边界，不替换真实来源、锁或提交协议。"""
        result=await original(*args)
        ready.set()
        await release.wait()
        return result
    monkeypatch.setattr(module,'snapshots',hold)
    pending=asyncio.create_task(operation())
    await asyncio.wait_for(ready.wait(),10)
    try:
        if action=='cancel':
            await service.act(w.who,record.definition_id,'cancel',{'request_id':'race-cancel','expected_version':1})
        else:
            body=record.confirmed.model_dump(mode='json')
            body.update(request_id='race-edit',expected_version=1)
            if mode=='time':body['trigger']['at']='2026-10-22T21:01:00+08:00'
            await service.update(w.who,record.definition_id,body)
    finally:
        release.set()
    result=await asyncio.wait_for(pending,10)
    assert not await service.tasks.store.list(w.who)
    if mode=='event':assert result.status=='cancelled' and result.reason_code=='definition_changed'
    else:assert result is None
    assert not w.harness.world.models.requests

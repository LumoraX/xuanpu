"""真实源事务写出可信事件，固定发生版本且逐条去重。"""
from __future__ import annotations

import pytest
from xuanpu_server.errors import InvalidRequestError

from .conftest import request_for


async def event_definition(w):
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    request=await request_for(w)
    source=request['template']['input_files'][0]['item_id']
    request.update(input_policy='latest_at_occurrence',trigger={'kind':'event',
        'event_type':'workspace.file_revision_committed','subject_id':source})
    record=await service.create(w.who,request)
    return service,record,source


async def test_file_event_uses_exact_revision_and_delivery_is_idempotent(trigger_world):
    """事件后又更新正本，首次实例仍只读取事件指定revision。"""
    w=trigger_world
    service,record,source=await event_definition(w)
    assert hasattr(w.platform.trigger_worker,'tick_events'),'尚无可信事件投递'
    item=await w.platform.workspace.get(w.who,source)
    item=await w.platform.workspace.replace_content(w.who,source,'部门,金额\nA,15.50\n'.encode(),'text/csv',item['version'],'v2')
    async with w.platform.workspace.transaction(w.who,'work-task:trigger-inspect') as state:
        event=next(iter(state.task_domain_events.values()))
        delivery=next(iter(state.task_event_deliveries.values()))
        assert event['input_files'][0]['revision']==2
        assert not {'text','title','name','content','api_key'} & event.keys()
    await w.platform.workspace.replace_content(w.who,source,'部门,金额\nA,99.00\n'.encode(),'text/csv',item['version'],'v3')
    await service.deliver_event(w.who,delivery['delivery_id'])
    await service.deliver_event(w.who,delivery['delivery_id'])
    values=await service.tasks.store.list(w.who)
    assert len(values)==1 and values[0].inputs[0].revision==2
    await w.platform.trigger_worker.tick_events()
    assert len(await service.tasks.store.list(w.who))==1


async def test_source_rollback_has_no_event_or_subscription_leak(trigger_world,monkeypatch):
    """源文件真实写流程的提交异常不能留下投递或新revision。"""
    w=trigger_world
    service,record,source=await event_definition(w)
    assert hasattr(w.platform.trigger_worker,'tick_events'),'尚无事务outbox'
    backend=w.platform.workspace.store
    if hasattr(backend,'_save'):
        original=backend._save
        async def fail(*args):
            await original(*args)
            raise OSError('source rollback')
        monkeypatch.setattr(backend,'_save',fail)
    else:
        def fail(*args):raise OSError('source rollback')
        monkeypatch.setattr(backend,'_audit',fail)
    # get本身也经事务；在故障注入前已知的第一版version固定为1。
    with pytest.raises(OSError):
        await w.platform.workspace.replace_content(w.who,source,b'new source','text/csv',1,'rollback-source')
    monkeypatch.undo()
    async with w.platform.workspace.transaction(w.who,'work-task:trigger-inspect') as state:
        assert len(state.versions[source])==1
        assert not state.task_domain_events and not state.task_event_deliveries


async def test_later_definition_and_client_metadata_cannot_forge_subscription(trigger_world):
    """没有公共发布事件入口，客户端也不能写清除可信来源的字段。"""
    w=trigger_world
    service,record,source=await event_definition(w)
    assert hasattr(w.platform.trigger_worker,'tick_events'),'尚无可信来源验证'
    with pytest.raises(InvalidRequestError):
        await w.platform.workspace.update(w.who,source,{'_trigger_origin_chain':[]},1)
    await service.act(w.who,record.definition_id,'pause',{'request_id':'pause','expected_version':1})
    await w.platform.workspace.replace_content(w.who,source,b'paused source','text/csv',1,'paused-version')
    current=await service.get(w.who,record.definition_id)
    await service.act(w.who,record.definition_id,'resume',{'request_id':'resume','expected_version':current.state_version})
    await w.platform.trigger_worker.tick_events()
    assert not await service.tasks.store.list(w.who)

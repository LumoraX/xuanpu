"""真实领域触发链及持久速率限制，结果文件不能无限反向触发。"""
from __future__ import annotations

import asyncio
import json

from xuanpu_server.work_tasks.registry import TaskRegistry

from ..work_tasks.test_phase_recovery import TABLE_STEPS
from .conftest import request_for


async def enable(w):
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    request=await request_for(w)
    request.update(trigger={'kind':'event','event_type':'workspace.file_revision_committed'},max_executions=100)
    record=await service.create(w.who,request)
    return service,record,request['template']['input_files'][0]['item_id']


async def test_twenty_fires_per_hour_persists_pause_and_one_notification(trigger_world):
    """取消已受理实例也不能清掉频率计数；再次扫描不重复通知。"""
    w=trigger_world
    service,record,source=await enable(w)
    for i in range(21):
        item=await w.platform.workspace.get(w.who,source)
        await w.platform.workspace.replace_content(w.who,source,'部门,金额\nA,15.50\n'.encode(),
                                                   'text/csv',item['version'],f'rate-{i}')
        await w.platform.trigger_worker.tick_events()
        for task in await service.tasks.store.list(w.who):
            if task.status=='accepted':await service.tasks.cancel(w.who,task.task_id)
        w.now+=1
    current=await service.get(w.who,record.definition_id)
    assert not current.enabled and current.pause_reason=='hourly_limit'
    assert len(await service.tasks.store.list(w.who))==20
    await w.platform.trigger_worker.tick_events()
    notifications=await w.platform.store.reminder_store.notifications('acme',w.who.user_id,after_id=0,limit=100)
    assert len([n for n in notifications if n.source=='task_definition'])==1


async def test_real_generated_artifact_events_do_not_reenter_origin_chain(trigger_world,step):
    """真实Linux脚本产出四文件和业务成功后，同链只记录跳过，不再调用模型。"""
    w=trigger_world
    service,record,source=await enable(w)
    old=w.platform.task_registry
    registry=TaskRegistry(service.tasks.store,w.platform.task_runner,process_owner=w.platform.instance_id,
        now=lambda:w.now,durable=service.tasks.durable,cleanup=old.cleanup,recover=old.recover,actions=old.actions)
    w.platform.task_registry=registry
    service.tasks.executor=registry
    w.platform.task_runner.artifacts.now=lambda:w.now
    w.platform.task_runner.artifacts.staging.now=lambda:w.now
    w.platform.task_runner.proposals.now=lambda:w.now
    await registry.open(scan=False)
    w.harness.world.models.script(*(step(**value) for value in TABLE_STEPS))
    await w.platform.workspace.replace_content(w.who,source,'部门,金额\nA,12.50\nB,3.00\n'.encode(),'text/csv',1,'source-v2')
    await w.platform.trigger_worker.tick_events()
    await asyncio.wait_for(registry.join(),30)
    tasks=await service.tasks.store.list(w.who)
    assert len(tasks)==1 and tasks[0].status=='succeeded',tasks
    evidence=next(a for a in tasks[0].artifacts if a.role=='evidence')
    data=(await w.platform.workspace.download(w.who,evidence.item_id))[0]
    assert json.loads(data)['totals'][0]['amount']=='15.50'
    calls=len(w.harness.world.models.requests)
    for _ in range(3):
        await w.platform.trigger_worker.tick_events()
        await registry.join()
    assert len(await service.tasks.store.list(w.who))==1 and len(w.harness.world.models.requests)==calls
    occurrences=await service.occurrences(w.who,record.definition_id)
    assert len([o for o in occurrences if o.reason_code=='same_origin_chain'])==4


async def test_three_real_success_hops_pause_fourth_definition(trigger_world,step):
    """三个真实任务成功链携带服务端来源，第四级没有任务且只通知一次。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    body=await request_for(w)
    first=await service.create(w.who,body)
    w.now+=60
    current_occurrence=await service.fire_time(w.who,first.definition_id)
    old=w.platform.task_registry
    registry=TaskRegistry(service.tasks.store,w.platform.task_runner,process_owner=w.platform.instance_id,
        now=lambda:w.now,durable=service.tasks.durable,cleanup=old.cleanup,recover=old.recover,actions=old.actions)
    w.platform.task_registry=registry
    w.platform.task_runner.artifacts.now=lambda:w.now
    w.platform.task_runner.artifacts.staging.now=lambda:w.now
    w.platform.task_runner.proposals.now=lambda:w.now
    await registry.open(scan=False)
    for depth in range(3):
        successor=await service.create(w.who,{**body,'request_id':f'chain-{depth}','title':f'第{depth+2}级',
            'trigger':{'kind':'event','event_type':'work_task.succeeded','subject_id':current_occurrence.task_id}})
        w.harness.world.models.script(*(step(**value) for value in TABLE_STEPS))
        service.tasks.executor=registry
        await service.wake(w.who,current_occurrence)
        await asyncio.wait_for(registry.join(),30)
        parent=await service.tasks.store.get(w.who,current_occurrence.task_id)
        assert parent.status=='succeeded' and len(parent.trigger_origin_chain)==depth+1,parent
        service.tasks.executor=None
        for _ in range(2):await w.platform.trigger_worker.tick_events()
        occurrences=await service.occurrences(w.who,successor.definition_id)
        assert len(occurrences)==1
        current_occurrence=occurrences[0]
    assert current_occurrence.status=='blocked' and current_occurrence.reason_code=='chain_depth'
    assert not (await service.get(w.who,successor.definition_id)).enabled
    assert len(await service.tasks.store.list(w.who))==3
    notifications=await w.platform.store.reminder_store.notifications('acme',w.who.user_id,after_id=0,limit=100)
    assert len([n for n in notifications if n.source=='task_definition'])==1

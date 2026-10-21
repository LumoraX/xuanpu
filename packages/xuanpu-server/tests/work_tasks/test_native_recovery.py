"""已冻结候选跨正常重建后只确认本人所选记录。"""
from __future__ import annotations

import asyncio
import json

from xuanpu_server.work_tasks.decisions import OfficeActionsDecision
from xuanpu_server.work_tasks.models import WorkTaskInput


async def test_native_confirmation_survives_rebuild_without_model(recovery_world,step):
    """纪要、引用和本人选择都有真实回执，重复确认只创建一次。"""
    h = recovery_world
    platform,who = h.platform,h.who
    text = '我负责整理需求。另一项工作尚未决定。'
    source = await platform.workspace.upload(who,'会议.txt',text.encode(),'text/plain','native-source')
    actions = [{'kind':'todo','title':'整理需求','owner':'self','source':{
        'input_name':'input_1.txt','start':0,'end':8,'quote':'我负责整理需求。'}},
        {'kind':'todo','title':'待本人选择','owner':'unresolved','source':{
        'input_name':'input_1.txt','start':9,'end':len(text),'quote':'另一项工作尚未决定。'}}]
    manifest = {'kind':'meeting_minutes','artifacts':[
        {'path':'outputs/minutes.md','name':'会议纪要.md','role':'minutes','mime_type':'text/markdown'},
        {'path':'outputs/actions.json','name':'行动项.json','role':'actions','mime_type':'application/json'}]}
    h.world.models.script(step(calls=[('child','call_skill',{'skill_id':'meeting-minutes','reason':'整理纪要'})]),
        step(calls=[('minutes','file_write',{'path':'outputs/minutes.md','content':'# 纪要\n'+text}),
            ('actions','file_write',{'path':'outputs/actions.json','content':json.dumps(actions,ensure_ascii=False)}),
            ('manifest','file_write',{'path':'outputs/manifest.json','content':json.dumps(manifest)})]),
        step(text='子skill完成'),step(text='请确认候选'))
    task = await platform.work_tasks.accept(who,WorkTaskInput.model_validate({'request_id':'native-recovery',
        'text':'整理纪要并提出候选', 'input_files':[{'item_id':source['id'],'revision':1}]}))
    h.task = task
    await asyncio.wait_for(platform.task_registry.join(),20)
    current = await h.read_task()
    assert current.status == 'waiting_user' and len(current.action_candidates) == 2
    calls = len(h.world.models.requests)
    await platform.close()
    await h.reopen()
    current = await h.read_task()
    class Forbidden:
        """确认事务独立于模型是否可用。"""
        async def create(self,*args):
            raise AssertionError('原生确认不应调用模型')
    h.platform.runtimes._models = Forbidden()
    decision = OfficeActionsDecision.model_validate({'kind':'office_actions','request_id':'select-once',
        'state_version':current.state_version,'choices':[{'action_id':current.action_candidates[0]['action_id'],
            'accepted':True,'title':'本人已编辑的需求任务'}]})
    result = await h.platform.work_tasks.decide(who,task.task_id,decision)
    repeated = await h.platform.work_tasks.decide(who,task.task_id,decision)
    assert result == repeated and result.status == 'succeeded'
    todos = await h.platform.workspace.list(who,kind='todo')
    assert len(todos) == 1 and todos[0]['data']['title'] == '本人已编辑的需求任务'
    assert len(h.world.models.requests) == calls

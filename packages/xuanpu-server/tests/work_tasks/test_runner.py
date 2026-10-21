"""在真实Docker和双后端上执行两条办公交付，不把模型文字当成成果。"""

from __future__ import annotations

import asyncio
import json

from xuanpu_server.work_tasks.decisions import OfficeActionsDecision
from xuanpu_server.work_tasks.models import WorkTaskInput


async def wait_for(predicate):
    """为后台证据设置明确等待期限，失败不能挂死测试。"""
    async def poll():
        """短间隔等待条件，执行所有权仍在平台。"""
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), 15)


async def test_table_root_completion_saves_real_artifacts(office_world, step):
    """子skill完成后仍running；根轮结束、冻结及保存后才成功。"""
    world, gate = office_world, asyncio.Event()
    platform, who = world.platform, world.office_user
    file = await platform.workspace.upload(who, '原始.csv', '部门,金额\nA,10.20\nA,2.30\nB,3.00\n'.encode(),
                                            'text/csv', 'source')
    world.models.script(
        step(calls=[('dispatch','call_skill',{'skill_id':'table-summary','reason':'需要真实脚本精确汇总',
                                            'args':{'input':'inputs/input_1.csv'}})]),
        step(calls=[('script','run_script',{'skill_id':'table-summary','script_name':'summarize','args':{
            'inputs':'["input_1.csv"]','group_column':'部门','amount_column':'金额','currency_column':''}})]),
        step(text='子skill文件已经生成'), step(text='根轮准备校验保存', wait=gate))
    task = await platform.work_tasks.accept(who, WorkTaskInput.model_validate({'request_id':'table',
        'text':'按部门汇总金额，保留证据', 'input_files':[{'item_id':file['id'],'revision':1}]}))
    try:
        await wait_for(lambda: len(world.models.requests) >= 4)
        current = await platform.work_tasks.store.get(who, task.task_id)
        assert current.status == 'running' and not current.artifacts
    finally:
        gate.set()
    await asyncio.wait_for(platform.task_registry.join(), 15)
    current = await platform.work_tasks.store.get(who, task.task_id)
    assert current.status == 'succeeded', [str(message.content) for request in world.models.requests
        for message in (request.messages or []) if message.role == 'tool'][-6:]
    assert len(current.artifacts) == 4 and current.outputs_verified
    receipt = next(entry for entry in current.artifacts if entry.role == 'evidence')
    content, _, _ = await platform.workspace.download(who, receipt.item_id)
    assert json.loads(content)['totals'][0]['amount'] == '15.50'
    assert platform.runtimes.live_runtimes() == 0
    assert await platform.store.count_active_sessions('acme') == 0
    assert not platform.task_runner.inputs.directory(task).exists()


async def test_meeting_waits_for_selected_native_action(office_world, step):
    """真实纪要文件和引用验证完成后等待人，确认原生待办不再调用模型。"""
    world, platform, who = office_world, office_world.platform, office_world.office_user
    text = '我负责整理需求。其他同事负责复核。'
    file = await platform.workspace.upload(who,'会议.txt',text.encode(),'text/plain','source')
    action = {'kind':'todo','title':'整理需求','owner':'self','source':{
        'input_name':'input_1.txt','start':0,'end':8,'quote':'我负责整理需求。'}}
    manifest = {'kind':'meeting_minutes','artifacts':[
        {'path':'outputs/minutes.md','name':'会议纪要.md','role':'minutes','mime_type':'text/markdown'},
        {'path':'outputs/actions.json','name':'行动项.json','role':'actions','mime_type':'application/json'}]}
    world.models.script(
        step(calls=[('dispatch','call_skill',{'skill_id':'meeting-minutes','reason':'整理会议并引用原文'})]),
        step(calls=[('minutes','file_write',{'path':'outputs/minutes.md','content':'# 会议纪要\n'+text}),
                    ('actions','file_write',{'path':'outputs/actions.json','content':json.dumps([action],ensure_ascii=False)}),
                    ('manifest','file_write',{'path':'outputs/manifest.json','content':json.dumps(manifest)})]),
        step(text='会议子skill完成'), step(text='请确认行动项'))
    task = await platform.work_tasks.accept(who,WorkTaskInput.model_validate({'request_id':'meeting',
        'text':'整理纪要并提出我的行动项','input_files':[{'item_id':file['id'],'revision':1}]}))
    await asyncio.wait_for(platform.task_registry.join(), 15)
    current = await platform.work_tasks.store.get(who,task.task_id)
    assert current.status == 'waiting_user' and len(current.artifacts) == 2, current
    assert await platform.workspace.list(who,kind='todo') == []
    count = len(world.models.requests)
    decision = OfficeActionsDecision.model_validate({'kind':'office_actions','request_id':'choose',
        'state_version':current.state_version,'choices':[{'action_id':current.action_candidates[0]['action_id'],
                                                        'accepted':True}]})
    completed = await platform.work_tasks.decide(who,task.task_id,decision)
    repeated = await platform.work_tasks.decide(who,task.task_id,decision)
    assert completed.status == 'succeeded' and completed == repeated
    assert len(await platform.workspace.list(who,kind='todo')) == 1
    assert len(world.models.requests) == count


async def test_table_modified_after_successful_script_cannot_be_saved(office_world, step):
    """真实脚本算完后模型改写报告，摘要回执不匹配，全部成果拒绝保存。"""
    world, platform, who = office_world, office_world.platform, office_world.office_user
    file = await platform.workspace.upload(who,'数据.csv','部门,金额\nA,1.00\n'.encode(),'text/csv','source')
    world.models.script(
        step(calls=[('dispatch','call_skill',{'skill_id':'table-summary','reason':'精确计算'})]),
        step(calls=[('script','run_script',{'skill_id':'table-summary','script_name':'summarize','args':{
            'inputs':'["input_1.csv"]','group_column':'部门','amount_column':'金额','currency_column':''}})]),
        step(text='子skill完成'),
        step(calls=[('tamper','file_write',{'path':'outputs/report.md','content':'模型虚构金额9999'})]),
        step(text='错误地声称已完成'))
    task = await platform.work_tasks.accept(who,WorkTaskInput.model_validate({'request_id':'tamper',
        'text':'按部门汇总','input_files':[{'item_id':file['id'],'revision':1}]}))
    await asyncio.wait_for(platform.task_registry.join(),15)
    current = await platform.work_tasks.store.get(who,task.task_id)
    assert current.status == 'failed' and not current.artifacts
    assert len(await platform.workspace.list(who,kind='file')) == 1

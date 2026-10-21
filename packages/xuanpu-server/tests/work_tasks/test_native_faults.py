"""办公原生确认沿用真实提醒配额、时间规则和同员工事务回滚。"""

from __future__ import annotations

import time

import pytest
from xuanpu_server.errors import ConflictError, InvalidRequestError
from xuanpu_server.reminders.models import ReminderInput
from xuanpu_server.work_tasks.decisions import OfficeActionsDecision
from xuanpu_server.work_tasks.models import ArtifactReceipt, InputFile, WorkTaskInput
from xuanpu_server.work_tasks.proposals import ActionProposalService


async def waiting_actions(world):
    """隔离验证成果核验后的原生事务；真实解析和文件另由runner用例覆盖。"""
    platform, who = world.platform, world.office_user
    await platform.task_registry.close();platform.work_tasks.executor = None
    item = await platform.workspace.upload(who,'确认材料.txt',b'self commitment','text/plain','native-source')
    task = await platform.work_tasks.accept(who, WorkTaskInput.model_validate({'request_id':'native-task',
        'text':'整理本人行动项','input_files':[{'item_id':item['id'],'revision':1}]}))
    now = time.time()
    receipt = ArtifactReceipt(item_id=item['id'], revision=1, sha256=task.inputs[0].sha256,
        size=15, mime_type='text/plain', input_refs=(InputFile(item_id=item['id'],revision=1),), generated_at=now)
    task = await platform.work_tasks.store.transition(who,task.task_id,'running',expected_version=1,
        at=now,artifacts=(receipt,),outputs_verified=True)
    service = ActionProposalService(platform.workspace,platform.work_tasks.store,reminders=platform.reminders)
    source = {'input_name':'input_1.txt','start':0,'end':4,'quote':'self'}
    entries = await service.prepare(who,task,[{'kind':'todo','title':'本人待办','owner':'self','source':source},
        {'kind':'event','title':'本人日程','owner':'self','source':source,
         'start_at':now+1200,'end_at':now+1800,'remind_at':now+600}],{'input_1.txt':'self commitment'})
    current = await service.store.get(who,task.task_id)
    task = await service.store.transition(who,task.task_id,'waiting_user',
        expected_version=current.state_version,at=time.time())
    return service, task, entries


async def test_reminder_quota_rolls_back_batch_then_same_decision_can_retry(office_world):
    """提醒已满时前半批待办也回滚；释放一个名额后同一决定只创建一次。"""
    world, platform, who = office_world, office_world.platform, office_world.office_user
    service, task, entries = await waiting_actions(world)
    existing = []
    for index in range(100):
        existing.append(await platform.reminders.create(who, ReminderInput(title='占用测试名额',
            due_at=time.time()+3600, timezone='Asia/Shanghai',channels=['inbox']),f'quota-{index}'))
    decision = OfficeActionsDecision.model_validate({'kind':'office_actions','request_id':'quota-confirm',
        'state_version':task.state_version,'choices':[{'action_id':entry['action_id'],'accepted':True}
                                                    for entry in entries]})
    with pytest.raises(ConflictError,match='100'):
        await service.confirm(who,task.task_id,decision)
    assert await platform.workspace.list(who,kind='todo') == []
    assert await platform.workspace.list(who,kind='event') == []
    assert (await service.store.get(who,task.task_id)).status == 'waiting_user'
    await platform.reminders.cancel(who,existing[0].reminder_id,existing[0].version)
    result = await service.confirm(who,task.task_id,decision)
    repeated = await service.confirm(who,task.task_id,decision)
    assert result == repeated and result.status == 'succeeded'
    assert len(await platform.workspace.list(who,kind='todo')) == 1
    assert len(await platform.workspace.list(who,kind='event')) == 1
    assert len(await platform.reminders.list(who,status='active')) == 100


@pytest.mark.parametrize('mode',['past_due','end_before_start','past_reminder','cancel_all'])
async def test_native_time_rules_or_explicit_cancel(office_world, mode):
    """过去时间和倒置日程不创建半批；全取消仍保留纪要且无原生记录。"""
    world, platform, who = office_world, office_world.platform, office_world.office_user
    service, task, entries = await waiting_actions(world)
    choices = [{'action_id':entry['action_id'],'accepted':True} for entry in entries]
    if mode == 'past_due':
        choices[0]['due_at'] = time.time()-60
    elif mode == 'end_before_start':
        choices[1]['end_at'] = time.time()+1000
    elif mode == 'past_reminder':
        choices[1]['remind_at'] = time.time()-60
    else:
        choices = []
    decision = OfficeActionsDecision.model_validate({'kind':'office_actions','request_id':'time-confirm',
        'state_version':task.state_version,'choices':choices})
    if mode == 'cancel_all':
        result = await service.confirm(who,task.task_id,decision)
        assert result.status == 'succeeded' and result.artifacts and not any(
            entry['accepted'] for entry in result.action_results)
    else:
        with pytest.raises(InvalidRequestError):
            await service.confirm(who,task.task_id,decision)
        assert (await service.store.get(who,task.task_id)).status == 'waiting_user'
    assert await platform.workspace.list(who,kind='todo') == []
    assert await platform.workspace.list(who,kind='event') == []
    assert await platform.reminders.list(who) == []

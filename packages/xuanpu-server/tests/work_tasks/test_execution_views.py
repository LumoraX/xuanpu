"""员工状态视图只展示可理解进展，不泄露执行凭据或参数。"""
from __future__ import annotations

import pytest
from xuanpu_server.errors import NotFoundError
from xuanpu_server.work_tasks.service import WorkTaskService

from .test_execution_store import prepared


async def test_view_has_safe_recovery_stage_and_original_event_cursor(task_env):
    """真实认领、阶段推进及预算阻塞对应准确视图与持久事件。"""
    env=task_env
    tasks,execution,task=await prepared(env)
    service=WorkTaskService(env.workspace,None,process_owner='p')
    lease=await execution.claim(env.who,task.task_id,owner='worker-private',now=1000.)
    await execution.checkpoint(lease,'executing',{'host_path':'/private/host','token':'PRIVATE_KEY'},now=1000.)
    await execution.release(lease,state_name='blocked',now=1000.,block_reason='budget_exhausted')
    view=await service.public_record(env.who,await tasks.get(env.who,task.task_id))
    assert view.get('execution_stage')=='executing','尚无统一的员工执行阶段'
    assert view['attempt_no']==1 and view['block_reason']=='budget_exhausted'
    assert view['next_run_at'] is None and not view['recovering']
    assert not {'process_owner','grant_id','staging_objects','frozen_staging','pending_decision','resume_evidence'} & view.keys()
    assert 'PRIVATE_KEY' not in str(view) and '/private/host' not in str(view)
    entries=await tasks.events(env.who,task.task_id)
    assert entries[0].event_kind=='accepted'
    assert any(e.execution_stage=='executing' and e.attempt_no==1 for e in entries)
    assert len(await tasks.events(env.who,task.task_id,after_id=entries[-1].event_id))==0


async def test_owner_view_cannot_use_another_users_record(task_env):
    """服务层序列化也复核当前员工，不能通过传入记录绕过所有权。"""
    from dataclasses import replace
    env=task_env
    tasks,_,task=await prepared(env)
    service=WorkTaskService(env.workspace,None,process_owner='p')
    with pytest.raises(NotFoundError):
        await service.public_record(replace(env.who,user_id='usr_b'),task)


async def test_real_tool_pending_id_survives_safe_serialization(task_env):
    """真实批准协议用request_id，隐私白名单不能删掉员工决定所需身份。"""
    from xuanpu_server.work_tasks.views import serialize
    task=task_env.record.model_copy(update={'status':'waiting_user','pending':({'request_id':'approval-1',
        'detail':{'scope':'tool_use','target':'private-tool','args':{'secret':'do-not-export'}}},)})
    result=serialize(task,None,[],set())
    assert result['pending']==[{'request_id':'approval-1','detail':{'scope':'tool_use'}}]

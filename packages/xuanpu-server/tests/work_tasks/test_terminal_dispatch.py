"""终态与派发索引同事务结清，历史残留只修索引而不重跑。"""
from __future__ import annotations

from dataclasses import replace

import pytest
from xuanpu_server.errors import ConflictError
from xuanpu_server.work_tasks.config import DurableExecutionConfig
from xuanpu_server.work_tasks.registry import TaskRegistry

from .test_execution_store import prepared


@pytest.mark.parametrize('old_state',['waiting_user','blocked','leased'])
async def test_startup_finds_and_repairs_old_terminal_dispatch(task_env,old_state):
    """模拟旧版本落下的索引，真实双后端启动枚举不漏读或发起模型。"""
    h=task_env;tasks,execution,task=await prepared(h)
    task=await tasks.transition(h.who,task.task_id,'cancelled',expected_version=task.state_version,at=1000.)
    async with h.workspace.transaction(h.who,'work-task:exec:'+task.task_id) as state:
        state.task_dispatch[task.task_id].update(state=old_state)
    assert (task.tenant_id,task.user_id) in await h.workspace.store.task_owners()
    assert [row.task_id for row in await tasks.lifecycle_records(task.tenant_id,task.user_id)]==[task.task_id]
    class Forbidden:
        """历史终态不得进入执行器。"""
        async def run(self,*args,**kwargs):
            """若修索引误变执行则立即失败。"""
            raise AssertionError('终态不能重新执行')
    registry=TaskRegistry(tasks,Forbidden(),process_owner='new',now=lambda:1001.,
        durable=DurableExecutionConfig(enabled=True,verified_text_bounds=True))
    await registry.reconcile(restart=True)
    dispatch=await execution.get(h.who,task.task_id)
    assert dispatch.state=='settled' and dispatch.expires_at==0 and dispatch.attempt_no==0
    assert (await tasks.get(h.who,task.task_id))==task
    assert await h.workspace.store.task_owners()==[]
    assert await tasks.lifecycle_records(task.tenant_id,task.user_id)==[]


@pytest.mark.parametrize('pending',[False,True])
async def test_terminal_release_preserves_epoch_and_cleanup_barrier(task_env,pending):
    """旧epoch依旧拒绝；原持有者释放后不把干净终态重新写成blocked。"""
    h=task_env;tasks,execution,task=await prepared(h)
    lease=await execution.claim(h.who,task.task_id,owner='p',now=1000.)
    assert lease is not None
    current=await tasks.get(h.who,task.task_id)
    if not pending:await tasks.resources_closed(current,at=1001.)
    current=await tasks.get(h.who,task.task_id)
    await tasks.transition(h.who,task.task_id,'interrupted',expected_version=current.state_version,at=1001.,error_code='grant_revoked')
    assert (await execution.get(h.who,task.task_id)).state=='leased'
    with pytest.raises(ConflictError):
        await execution.release(replace(lease,epoch=lease.epoch+1),state_name='blocked',now=1001.)
    await execution.release(lease,state_name='blocked',block_reason='grant_revoked',now=1001.)
    assert (await execution.get(h.who,task.task_id)).state==('blocked' if pending else 'settled')
    if pending:
        await tasks.resources_closed(await tasks.get(h.who,task.task_id),at=1002.)
        assert (await execution.get(h.who,task.task_id)).state=='settled'

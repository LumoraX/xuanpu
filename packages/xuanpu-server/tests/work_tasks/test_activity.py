"""有意义的工作通知在原事务持久化，重复恢复与轮询不刷屏。"""
from __future__ import annotations

from .test_execution_store import prepared


async def test_block_and_final_failure_notify_once_in_original_transaction(task_env):
    """实际派发变更触发通知；相同阻塞重放和资源收尾不重复。"""
    env=task_env
    backend=env.workspace.store
    assert hasattr(backend,'configure_activity'),'尚无默认关闭的工作动态开关'
    from xuanpu_server.work_tasks.activity import ActivityConfig
    backend.configure_activity(ActivityConfig(enabled=True))
    tasks,execution,task=await prepared(env)
    lease=await execution.claim(env.who,task.task_id,owner='p',now=1000.)
    await execution.release(lease,state_name='blocked',now=1000.,block_reason='budget_exhausted')
    await execution.release(lease,state_name='blocked',now=1000.,block_reason='budget_exhausted')
    await tasks.transition(env.who,task.task_id,'failed',expected_version=(await tasks.get(env.who,task.task_id)).state_version,at=1001.,error_code='attempts_exhausted')
    await tasks.resources_closed(await tasks.get(env.who,task.task_id),at=1001.)
    notifications=await env.control.reminder_store.notifications('acme','usr_a',0,100)
    owned=[n for n in notifications if n.source=='work_task']
    assert len(owned)==2 and all(n.work_task_id==task.task_id for n in owned)
    assert 'private' not in str(owned)


async def test_activity_default_off_does_not_retroactively_notify(task_env):
    """不开关不新增通知，后开也不把旧失败任务当新结果。"""
    env=task_env
    tasks,_,task=await prepared(env)
    await tasks.transition(env.who,task.task_id,'failed',expected_version=1,at=1001.,error_code='failed')
    assert await env.control.reminder_store.notifications('acme','usr_a',0,100)==[]

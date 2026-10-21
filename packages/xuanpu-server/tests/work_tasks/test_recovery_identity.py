"""恢复复核原key、员工角色和授权，不沿用失效身份。"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from xuanpu_server.tenancy import ApiKeyRecord, generate_api_key
from xuanpu_server.work_tasks.config import DurableExecutionConfig
from xuanpu_server.work_tasks.registry import TaskRegistry
from xuanpu_server.work_tasks.store import WorkTaskStore


async def test_revoked_origin_key_blocks_before_new_attempt(task_env):
    """真实key存储吊销后重新打开Registry，模型和容器均不能启动。"""
    env = task_env
    _, key_id, secret_hash = generate_api_key()
    await env.control.create_api_key(ApiKeyRecord(key_id=key_id, tenant_id='acme', name='交办来源',
        role='tenant_member', secret_hash=secret_hash, created_at=1000., can_act_as_user=True))
    who = replace(env.who, key_id=key_id)
    tasks = WorkTaskStore(env.workspace)
    task,_ = await tasks.create_or_replay(who,env.record.model_copy(update={
        'actor':who.actor,'recovery_policy_version':2}))
    await env.control.revoke_api_key('acme',key_id,at=1001.)
    calls = []
    async def forbidden(*args):
        calls.append('called')
        raise AssertionError('吊销授权不能再执行')
    registry = TaskRegistry(tasks, SimpleNamespace(run=forbidden), process_owner='new', now=lambda:1002.,
                            durable=DurableExecutionConfig(enabled=True))
    try:
        await registry.open(scan=False)
        await registry.reconcile()
        await registry.join()
        dispatch = await registry.durable.execution.get(env.who,task.task_id)
        assert dispatch.block_reason == 'grant_revoked' and dispatch.attempt_no == 0
        assert calls == []
    finally:
        await registry.close()

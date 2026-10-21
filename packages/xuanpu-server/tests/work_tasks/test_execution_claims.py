"""原子全局准入和员工/租户边界，不依赖Registry内存计数。"""
from __future__ import annotations

import asyncio
import hashlib

from xuanpu_server.identity.models import UserRecord
from xuanpu_server.tenancy import Principal, Tenant
from xuanpu_server.work_tasks.execution_store import ExecutionStore
from xuanpu_server.work_tasks.models import InputSnapshot
from xuanpu_server.work_tasks.store import WorkTaskStore


async def test_atomic_global_limit_across_tenants_and_employees(task_env):
    """九名员工跨三租户同时认领，只有八个attempt可成立。"""
    env = task_env
    tasks,execution = WorkTaskStore(env.workspace),ExecutionStore(env.workspace)
    candidates = []
    for index in range(9):
        tenant,user = 'company-'+str(index//3),'usr_'+str(index)
        if index%3 == 0:
            await env.control.create_tenant(Tenant(tenant_id=tenant,name='合成公司',created_at=1.))
        await env.identity.create_user(UserRecord(tenant_id=tenant,user_id=user,display_name='员工',created_at=1.))
        who = Principal(role='tenant_member',tenant_id=tenant,user_id=user,key_id=None)
        data = b'bounded source'
        file = await env.workspace.upload(who,'固定材料',data,'text/plain','source')
        source = InputSnapshot(item_id=file['id'],revision=1,size=len(data),sha256=hashlib.sha256(data).hexdigest(),
            mime_type='text/plain',relative_name='input_1.txt')
        record = env.record.model_copy(update={'task_id':'task_'+str(index),'tenant_id':tenant,'user_id':user,
            'actor':who.actor,'inputs':(source,),'recovery_policy_version':2})
        accepted,_ = await tasks.create_or_replay(who,record)
        candidates.append((who,accepted))
    leases = await asyncio.wait_for(asyncio.gather(*(execution.claim(who,task.task_id,owner='worker',now=1000.)
        for who,task in candidates)),10)
    assert sum(lease is not None for lease in leases) == 8
    for (who,task),lease in zip(candidates,leases,strict=True):
        assert (await execution.get(who,task.task_id)).attempt_no == (1 if lease else 0)


async def test_one_employee_cannot_claim_two_compute_tasks(task_env):
    """员工锁内统计其他活跃派发，没有名额不消耗第二任务次数。"""
    from .test_execution_store import prepared

    tasks,execution,task = await prepared(task_env)
    second,_ = await tasks.create_or_replay(task_env.who,task_env.record.model_copy(update={
        'task_id':'second','request_id':'second','recovery_policy_version':2}))
    first = await execution.claim(task_env.who,task.task_id,owner='p',now=1000.)
    assert first is not None
    assert await execution.claim(task_env.who,second.task_id,owner='p',now=1000.) is None
    assert (await execution.get(task_env.who,second.task_id)).attempt_no == 0

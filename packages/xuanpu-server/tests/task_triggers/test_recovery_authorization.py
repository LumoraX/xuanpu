"""停机恢复、来源授权与逐任务重试共享总账。"""
from __future__ import annotations

from dataclasses import replace

import pytest
from xuanpu_server.errors import NotFoundError
from xuanpu_server.task_triggers.service import TaskDefinitionService
from xuanpu_server.task_triggers.worker import TriggerWorker
from xuanpu_server.tenancy import ApiKeyRecord, Principal, generate_api_key
from xuanpu_server.work_tasks.execution_store import ExecutionStore

from .conftest import request_for


@pytest.mark.parametrize('policy,expected',[('skip','missed'),('coalesce_latest','accepted')])
async def test_three_days_offline_reopens_one_occurrence(trigger_world,policy,expected):
    """重建服务和扫描器使用同一已提交库；不追赶三天全部时刻。"""
    w=trigger_world
    old=w.platform.task_definitions
    old.config=old.config.model_copy(update={'enabled':True})
    old.now=lambda:w.now
    body=await request_for(w,kind='daily')
    body['misfire_policy']=policy
    first=await old.create(w.who,body)
    await w.platform.trigger_worker.close()
    w.now+=3*86400+3600
    service=TaskDefinitionService(old.tasks,now=lambda:w.now,config=old.config)
    worker=TriggerWorker(service)
    for _ in range(3):await worker.tick_time()
    occurrences=await service.occurrences(w.who,first.definition_id)
    assert len(occurrences)==1 and occurrences[0].status==expected
    assert len(await service.tasks.store.list(w.who))==(expected=='accepted')
    assert (await service.get(w.who,first.definition_id)).next_fire_at>w.now
    # 时钟倒退也不重复已发生的稳定时刻。
    w.now-=7200
    await worker.tick_time()
    assert len(await service.occurrences(w.who,first.definition_id))==1


async def test_revoked_original_key_cannot_materialize(trigger_world):
    """原代员工key撤销后，扫描器身份不能代替其授权。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    _,key,digest=generate_api_key()
    await w.platform.store.create_api_key(ApiKeyRecord(key_id=key,tenant_id='acme',name='触发授权',
        role='tenant_member',secret_hash=digest,created_at=w.now,can_act_as_user=True))
    first=await service.create(replace(w.who,key_id=key),await request_for(w))
    await w.platform.store.revoke_api_key('acme',key,at=w.now)
    w.now+=60
    result=await service.fire_time(w.who,first.definition_id)
    assert result.status=='blocked' and result.reason_code=='grant_revoked'
    assert not (await service.get(w.who,first.definition_id)).enabled
    assert not await service.tasks.store.list(w.who)


async def test_definition_owner_boundary_and_shared_attempt_limit(trigger_world):
    """他人404；每实例一次重试不能消耗独立grant的剩余执行配额。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    body=await request_for(w,kind='daily')
    body['max_attempts']=1
    first=await service.create(w.who,body)
    stranger=Principal(role='tenant_admin',tenant_id='acme',user_id='usr_foreign',key_id=None)
    from xuanpu_server.identity.models import UserRecord
    await w.platform.identity.store.create_user(UserRecord(tenant_id='acme',user_id=stranger.user_id,display_name='其他管理员',created_at=w.now))
    with pytest.raises(NotFoundError):await service.get(stranger,first.definition_id)
    assert await service.list(stranger)==[]
    w.now+=60
    occurrence=await service.fire_time(w.who,first.definition_id)
    execution=ExecutionStore(w.platform.workspace)
    lease=await execution.claim(w.who,occurrence.task_id,owner='first',now=w.now)
    current=await service.tasks.store.get(w.who,occurrence.task_id)
    await service.tasks.store.resources_closed(current,at=w.now)
    await execution.release(lease,state_name='retry_wait',now=w.now,due_at=w.now)
    assert await execution.claim(w.who,occurrence.task_id,owner='retry',now=w.now) is None
    grant=await execution.grant(w.who,first.grant_id)
    assert grant.attempts_used==1 and grant.max_attempts==3


async def test_unsettled_event_backlog_has_no_content(trigger_world):
    """诊断提供积压及最老时间，不泄露事件标识、材料名称或正文。"""
    from .test_event_occurrences import event_definition
    w=trigger_world
    service,_,source=await event_definition(w)
    await w.platform.workspace.replace_content(w.who,source,b'PRIVATE content','text/csv',1,'backlog')
    assert hasattr(service.workspace.store,'trigger_backlog'),'尚无持久积压观测'
    data=await service.workspace.store.trigger_backlog()
    assert data=={'unsettled':1,'oldest_at':w.now}
    await w.platform.trigger_worker.tick_events()
    assert (await service.workspace.store.trigger_backlog())['unsettled']==0

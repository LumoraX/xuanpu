"""审查发现的真实授权和暂停恢复边界。"""
from __future__ import annotations

from dataclasses import replace

from xuanpu_server.tenancy import ApiKeyRecord, generate_api_key

from .conftest import request_for


async def test_time_acceptance_preserves_original_delegating_key(trigger_world):
    """扫描器不是原发起人，受理仍须沿原grant身份进入R2-A。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    _,key_id,digest=generate_api_key()
    await w.platform.store.create_api_key(ApiKeyRecord(key_id=key_id,tenant_id='acme',name='委托来源',
        role='tenant_member',secret_hash=digest,created_at=w.now,can_act_as_user=True))
    original=replace(w.who,key_id=key_id)
    record=await service.create(original,await request_for(w))
    w.now+=60
    await service.fire_time(w.who,record.definition_id)
    tasks=await service.tasks.store.list(w.who)
    assert len(tasks)==1 and tasks[0].actor==original.actor


async def test_pause_of_quota_blocked_time_keeps_stable_occurrence_recoverable(trigger_world):
    """暂停不把可恢复的资格阻塞写成取消墓碑。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    record=await service.create(w.who,await request_for(w))
    w.now+=60
    service.tasks.store.max_tasks=0
    blocked=await service.fire_time(w.who,record.definition_id)
    assert blocked.status=='blocked'
    paused=await service.act(w.who,record.definition_id,'pause',{'request_id':'pause','expected_version':1})
    await service.act(w.who,record.definition_id,'resume',{'request_id':'resume','expected_version':paused.state_version})
    service.tasks.store.max_tasks=1000
    w.now+=60
    accepted=await service.fire_time(w.who,record.definition_id)
    assert accepted.status=='accepted' and accepted.occurrence_id==blocked.occurrence_id


async def test_update_replay_survives_past_due_and_feature_off(trigger_world):
    """响应丢失后的原请求重试应回放提交结果，不重新验证未来时刻。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    request=await request_for(w)
    first=await service.create(w.who,request)
    edit={**request,'request_id':'edit-replay','expected_version':1}
    changed=await service.update(w.who,first.definition_id,edit)
    w.now+=120
    service.config=service.config.model_copy(update={'enabled':False})
    again=await service.update(w.who,first.definition_id,edit)
    assert again==changed


async def test_definition_cancel_includes_old_version_inflight_grant(trigger_world):
    """新版本确认不抹掉旧版在途责任，取消整个定义必须覆盖全部版本。"""
    from xuanpu_server.work_tasks.execution_store import ExecutionStore
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    request=await request_for(w)
    first=await service.create(w.who,request)
    w.now+=60
    occurrence=await service.fire_time(w.who,first.definition_id)
    execution=ExecutionStore(w.platform.workspace)
    assert await execution.claim(w.who,occurrence.task_id,owner='old',now=w.now) is not None
    changed=await service.update(w.who,first.definition_id,{**request,'request_id':'new-version','expected_version':1,
        'trigger':{'kind':'time','repeat':'once','at':'2026-10-22T21:01:00+08:00'}})
    await service.act(w.who,first.definition_id,'cancel',{'request_id':'cancel-all','expected_version':changed.state_version})
    current=await service.tasks.store.get(w.who,occurrence.task_id)
    assert current.cancel_requested
    assert (await execution.grant(w.who,first.grant_id)).revoked


async def test_pause_preserves_lease_but_blocks_new_attempt(trigger_world):
    """当前attempt可收尾，释放后不能靠重试绕过暂停状态。"""
    from xuanpu_server.work_tasks.execution_store import ExecutionStore
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    first=await service.create(w.who,await request_for(w))
    w.now+=60
    occurrence=await service.fire_time(w.who,first.definition_id)
    execution=ExecutionStore(w.platform.workspace)
    lease=await execution.claim(w.who,occurrence.task_id,owner='old',now=w.now)
    await service.act(w.who,first.definition_id,'pause',{'request_id':'pause-lease','expected_version':1})
    current=await service.tasks.store.get(w.who,occurrence.task_id)
    assert not current.cancel_requested
    await service.tasks.store.resources_closed(current,at=w.now)
    await execution.release(lease,state_name='retry_wait',now=w.now,due_at=w.now)
    assert await execution.claim(w.who,occurrence.task_id,owner='retry',now=w.now) is None


async def test_duplicate_claim_after_pause_preserves_active_owner(trigger_world):
    """重复扫描不得把有效租约改成blocked，默认暂停允许原owner继续。"""
    from xuanpu_server.work_tasks.execution_store import ExecutionStore
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    first=await service.create(w.who,await request_for(w))
    w.now+=60
    occurrence=await service.fire_time(w.who,first.definition_id)
    execution=ExecutionStore(w.platform.workspace)
    lease=await execution.claim(w.who,occurrence.task_id,owner='original',now=w.now)
    await service.act(w.who,first.definition_id,'pause',{'request_id':'pause-live','expected_version':1})
    assert await execution.claim(w.who,occurrence.task_id,owner='duplicate',now=w.now) is None
    renewed=await execution.renew(lease,now=w.now+1)
    assert renewed.owner=='original' and renewed.epoch==lease.epoch


async def test_resume_replay_is_available_with_feature_off(trigger_world):
    """已提交的恢复回执不因后来关闭开关丢失。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    first=await service.create(w.who,await request_for(w))
    paused=await service.act(w.who,first.definition_id,'pause',{'request_id':'p','expected_version':1})
    body={'request_id':'r','expected_version':paused.state_version}
    resumed=await service.act(w.who,first.definition_id,'resume',body)
    service.config=service.config.model_copy(update={'enabled':False})
    assert await service.act(w.who,first.definition_id,'resume',body)==resumed


async def test_event_flag_race_keeps_delivery_retryable(trigger_world,monkeypatch):
    """锁外读取时关闭开关，提交只能暂存，不能把未改变定义的事件吞掉。"""
    from xuanpu_server.task_triggers import event_service

    from .test_event_occurrences import event_definition
    w=trigger_world
    service,_,source=await event_definition(w)
    await w.platform.workspace.replace_content(w.who,source,b'A,15.50','text/csv',1,'flag-source')
    async with w.platform.workspace.transaction(w.who,'work-task:trigger-inspect') as state:
        identifier=next(iter(state.task_event_deliveries))
    original=event_service.snapshots
    async def close_during_read(*args):
        value=await original(*args)
        service.config=service.config.model_copy(update={'enabled':False})
        return value
    monkeypatch.setattr(event_service,'snapshots',close_during_read)
    result=await service.deliver_event(w.who,identifier)
    assert result.status=='blocked' and result.reason_code=='feature_disabled'
    monkeypatch.undo()
    service.config=service.config.model_copy(update={'enabled':True})
    w.now+=60
    await w.platform.trigger_worker.tick_events()
    assert len(await service.tasks.store.list(w.who))==1


async def test_disabled_employee_revokes_all_versions_even_when_paused(trigger_world):
    """停用回调覆盖暂停状态和旧版本，不留下可重新启用的独立grant。"""
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    body=await request_for(w)
    first=await service.create(w.who,body)
    changed=await service.update(w.who,first.definition_id,{**body,'request_id':'v2-disable','expected_version':1})
    await service.act(w.who,first.definition_id,'pause',{'request_id':'p-disable','expected_version':changed.state_version})
    await w.platform.identity.disable_user('acme',w.who.user_id,'admin',w.harness.world.admin,keep_an_admin=True)
    from xuanpu_server.work_tasks.store import _lifecycle_guard
    async with service.workspace.store.transaction('acme',w.who.user_id,_lifecycle_guard,'work-task:trigger-inspect',allow_inactive=True) as state:
        assert all(g['revoked'] and not g['accepting_new_tasks'] for g in state.task_grants.values())
        assert state.task_definitions[first.definition_id]['pause_reason']=='grant_revoked'


async def test_resume_and_update_cannot_exceed_enabled_definition_cap(trigger_world):
    """暂停腾出的名额被占用后，恢复和重新确认都须复核相同100项上限。"""
    import pytest
    from xuanpu_server.errors import ConflictError
    w=trigger_world
    service=w.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    body=await request_for(w)
    first=await service.create(w.who,body)
    paused=await service.act(w.who,first.definition_id,'pause',{'request_id':'cap-pause','expected_version':1})
    for i in range(100):
        await service.create(w.who,{**body,'request_id':f'cap-{i}'})
    with pytest.raises(ConflictError):
        await service.act(w.who,first.definition_id,'resume',{'request_id':'cap-resume','expected_version':paused.state_version})
    with pytest.raises(ConflictError):
        await service.update(w.who,first.definition_id,{**body,'request_id':'cap-edit','expected_version':paused.state_version})
    assert not (await service.get(w.who,first.definition_id)).enabled

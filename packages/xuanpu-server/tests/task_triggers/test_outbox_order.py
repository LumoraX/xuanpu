"""两个真实PG事务乱序提交，小序号事件仍必须投递。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.tenancy import Principal

from .conftest import request_for
from .test_event_occurrences import event_definition


@pytest.mark.parametrize('identity_stores',['postgres'],indirect=True)
async def test_lower_sequence_late_commit_is_not_lost(trigger_world,monkeypatch):
    """不同员工锁、不同PG连接产生真实乱序，不能在同事务伪造序号。"""
    w=trigger_world
    service,first,source1=await event_definition(w)
    other=Principal(role='tenant_member',tenant_id='acme',user_id='usr_zz_late',key_id=None)
    await w.platform.identity.store.create_user(UserRecord(tenant_id='acme',user_id=other.user_id,display_name='第二员工',created_at=w.now))
    w2=SimpleNamespace(platform=w.platform,who=other,now=w.now)
    request=await request_for(w2)
    source2=request['template']['input_files'][0]['item_id']
    request.update(input_policy='latest_at_occurrence',trigger={'kind':'event',
        'event_type':'workspace.file_revision_committed','subject_id':source2})
    await service.create(other,request)
    backend=w.platform.workspace.store
    saved,release=asyncio.Event(),asyncio.Event()
    original=backend._save
    first_sequence=None
    used=False
    async def hold(conn,tenant,user,old,state):
        nonlocal first_sequence,used
        await original(conn,tenant,user,old,state)
        events=[e for e in state.task_domain_events.values() if e['subject_id']==source1 and e['subject_version']==2]
        if not used and events:
            used=True
            first_sequence=await conn.fetchval(f"SELECT sequence_id FROM {backend._t['task_domain_event']} WHERE event_id=$1",events[0]['event_id'])
            saved.set()
            await release.wait()
    monkeypatch.setattr(backend,'_save',hold)
    operation=asyncio.create_task(w.platform.workspace.replace_content(w.who,source1,'部门,金额\nA,1.00\n'.encode(),'text/csv',1,'late-a'))
    try:
        await asyncio.wait_for(saved.wait(),10)
        await w.platform.workspace.replace_content(other,source2,'部门,金额\nA,2.00\n'.encode(),'text/csv',1,'early-b')
        async with backend._pool.acquire() as conn:
            second_sequence=await conn.fetchval(f"SELECT sequence_id FROM {backend._t['task_domain_event']} WHERE user_id=$1",other.user_id)
        assert first_sequence<second_sequence
        await w.platform.trigger_worker.tick_events()
        assert len(await service.tasks.store.list(other))==1
    finally:
        release.set()
        await asyncio.wait_for(operation,10)
    await w.platform.trigger_worker.tick_events()
    await w.platform.trigger_worker.tick_events()
    assert len(await service.tasks.store.list(w.who))==1
    assert len(await service.tasks.store.list(other))==1


async def test_expired_delivery_cannot_commit_after_new_owner(trigger_world,monkeypatch):
    """原事件处理者的迟到结果不能绕过新epoch再次受理。"""
    from xuanpu_server.errors import ConflictError
    from xuanpu_server.task_triggers import event_service
    w=trigger_world
    service,record,source=await event_definition(w)
    await w.platform.workspace.replace_content(w.who,source,'部门,金额\nA,3.00\n'.encode(),'text/csv',1,'delivery-source')
    async with w.platform.workspace.transaction(w.who,'work-task:trigger-inspect') as state:
        identifier=next(iter(state.task_event_deliveries))
    original=event_service.snapshots
    started,release=asyncio.Event(),asyncio.Event()
    used=False
    async def snapshot(*args):
        nonlocal used
        value=await original(*args)
        if not used:
            used=True
            started.set()
            await release.wait()
        return value
    monkeypatch.setattr(event_service,'snapshots',snapshot)
    old=asyncio.create_task(service.deliver_event(w.who,identifier))
    await asyncio.wait_for(started.wait(),10)
    w.now+=61
    await service.deliver_event(w.who,identifier)
    release.set()
    with pytest.raises(ConflictError,match='stale_delivery'):
        await old
    assert len(await service.tasks.store.list(w.who))==1

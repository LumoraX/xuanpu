"""主动业务须从对话承诺走到等待、到期核实和完成停止。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.proactive.models import ActionInput
from xuanpu_server.proactive.service import ProactiveService
from xuanpu_server.tenancy import Principal, Tenant
from xuanpu_server.workspace.service import WorkspaceService


@pytest.fixture
async def business(identity_stores, tmp_path):
    """内存和真实数据库共用业务测试与可推进时钟。"""
    control, identity = identity_stores
    for tenant in ('acme','other'):
        await control.create_tenant(Tenant(tenant_id=tenant,name=tenant,created_at=1.0))
        await identity.create_user(UserRecord(tenant_id=tenant,user_id='a',display_name='甲',created_at=1.0))
    clock=[1800000000.0]
    workspace=WorkspaceService(store=control.workspace_store,control=control,identity=identity,
        objects=LocalObjectStore(tmp_path/'objects'),now=lambda:clock[0])
    outputs=[]
    sampled=[]
    async def sample(tenant,user,payload):
        """替代供应商边界，仍检查真实证据输入与提交。"""
        sampled.append(json.loads(payload))
        return json.dumps(outputs.pop(0),ensure_ascii=False)
    service=ProactiveService(workspace=workspace,control=control,identity=identity,
        sample=sample,now=lambda:clock[0])
    who=Principal('tenant_member','acme',None,'a')
    return service,workspace,control,identity,who,clock,outputs,sampled


def candidate(**changes):
    """语义结果只引用模型实际收到的用户消息。"""
    return {'followups':[dict(dict(title='发报价',intent_type='commitment',certainty='explicit',
        reason='用户说下午把报价发给客户，尚未看到完成记录',evidence_refs=['message:1'],
        item_id=None,followup_id=None,proposed_time=None,next_review_at=None,
        suggested_action='ask_status'),**changes)]}


async def test_conversation_to_wait_due_and_stop(business):
    """未建待办也会主动核实，用户安排后等待，到点跟进，完成后停止。"""
    service,workspace,control,_,who,clock,outputs,sampled=business
    await workspace.append_message(who,'user','下午把报价发给客户','commit:user')
    outputs.append(candidate())
    assert await service.evaluate_owner('acme','a') is True
    items=await service.list(who)
    assert len(items)==1 and items[0]['evidence_refs']==['message:1']
    assert 'user' in json.dumps(sampled[0]) and len(await workspace.list(who,kind='todo'))==0
    follow=items[0]
    due=clock[0]+60
    action=ActionInput(request_id='arrange',version=follow['version'],action='postpone',due_at=due)
    waiting=await service.act(who,follow['id'],action)
    assert waiting['status']=='waiting' and waiting['next_review_at']==due
    assert await service.act(who,follow['id'],action)==waiting
    await service.scan_owner('acme','a')
    assert len(await control.reminder_store.notifications('acme','a',0,100))==1
    clock[0]=due+1
    await asyncio.gather(service.scan_owner('acme','a'),service.scan_owner('acme','a'))
    assert len(await control.reminder_store.notifications('acme','a',0,100))==2
    latest=(await service.list(who))[0]
    done=await service.act(who,latest['id'],ActionInput(request_id='done',version=latest['version'],action='complete'))
    assert done['status']=='resolved'
    clock[0]+=10*86400
    await service.scan_owner('acme','a')
    assert len(await control.reminder_store.notifications('acme','a',0,100))==2


async def test_rejects_invented_or_foreign_evidence(business):
    """不允许模型引用未提供的他人消息或凭空编造完成事实。"""
    service,workspace,control,_,who,_,outputs,_=business
    await workspace.append_message(who,'user','下午发报价','x:user')
    outputs.append(candidate(evidence_refs=['message:999']))
    assert await service.evaluate_owner('acme','a') is False
    assert await service.list(who)==[]
    assert await control.reminder_store.notifications('acme','a',0,100)==[]


async def test_model_failure_keeps_rules_running(business):
    """模型无有效结果不冒充成功，开放待办的规则提醒仍然工作。"""
    service,workspace,control,_,who,clock,outputs,_=business
    item=await workspace.create(who,'todo',{'title':'交报告','due_at':clock[0]-1},'task')
    await workspace.append_message(who,'user','记得交报告','x:user')
    outputs.append({'invalid':'output'})
    assert await service.evaluate_owner('acme','a') is False
    await service.scan_owner('acme','a')
    notifications=await control.reminder_store.notifications('acme','a',0,100)
    assert len(notifications)==1 and notifications[0].source=='proactive_check'
    await workspace.update(who,item['id'],{'status':'done'},item['version'])
    clock[0]+=86400
    await service.scan_owner('acme','a')
    assert len(await control.reminder_store.notifications('acme','a',0,100))==1


async def test_metadata_changes_do_not_repeat_stale_stage(business):
    """修改标题不能冒充工作进展，也不能重复同一阶段催办。"""
    service,workspace,control,_,who,clock,_,_=business
    item=await workspace.create(who,'todo',{'title':'核对清单'},'task')
    clock[0]+=3*86400+1
    await service.scan_owner('acme','a')
    assert len(await control.reminder_store.notifications('acme','a',0,100))==1
    edited=await workspace.update(who,item['id'],{'title':'新的标题'},item['version'])
    await service.scan_owner('acme','a')
    assert len(await control.reminder_store.notifications('acme','a',0,100))==1
    check=(await service.list(who))[0]
    with pytest.raises(ConflictError):
        await service.act(who,check['id'],ActionInput(request_id='old',version=check['version']+1,action='complete'))
    assert edited['last_progress_at']==item['last_progress_at']


async def test_owner_scope_and_disable(business):
    """跨员工不可见，停用后不得新增模型评估或通知。"""
    service,workspace,control,identity,who,clock,outputs,sampled=business
    await workspace.append_message(who,'user','下午发报价','x:user')
    outputs.append(candidate())
    await service.evaluate_owner('acme','a')
    record=(await service.list(who))[0]
    other=replace(who,tenant_id='other')
    assert await service.list(other)==[]
    with pytest.raises(NotFoundError):
        await service.act(other,record['id'],ActionInput(request_id='foreign',version=record['version'],action='complete'))
    await identity.set_user_status('acme','a','disabled',reason='admin',at=clock[0])
    clock[0]+=86400
    assert await service.evaluate_owner('acme','a') is False
    assert len(sampled)==1


async def test_concurrent_semantic_claim_is_once(business):
    """持久认领在两个 worker 竞争时只调用一次模型。"""
    service,workspace,_,_,who,_,outputs,sampled=business
    await workspace.append_message(who,'user','下午发报价','x:user')
    outputs.extend([candidate(),candidate()])
    await asyncio.gather(service.evaluate_owner('acme','a'),service.evaluate_owner('acme','a'))
    assert len(sampled)==1 and len(await service.list(who))==1


async def test_truncated_backlog_is_not_lost(business):
    """32KiB截断只消费实际提供的消息，空结果也可继续下一页。"""
    service,workspace,_,_,who,clock,outputs,sampled=business
    for i in range(5):
        await workspace.append_message(who,'user','一份长业务记录'*500,f'long:{i}')
    outputs.append({'followups':[]})
    assert await service.evaluate_owner('acme','a')
    async with service.transaction('acme','a') as state:
        cursor=state.scan_state['message_cursor']
        offset=state.scan_state['partial_offsets'].get('message:1',0)
    assert cursor < 5 and sampled[0]['truncated']
    clock[0]+=1801
    outputs.append({'followups':[]})
    assert await service.evaluate_owner('acme','a')
    async with service.transaction('acme','a') as state:
        assert state.scan_state['message_cursor'] > cursor or state.scan_state['partial_offsets'].get('message:1',0)>offset


async def test_periodic_review_keeps_old_commitment_evidence(business):
    """没有新聊天也要能重新核实原承诺，不以空上下文冒充检查。"""
    service,workspace,control,_,who,clock,outputs,sampled=business
    await workspace.append_message(who,'user','下午把报价发给客户','old:user')
    outputs.append(candidate())
    await service.evaluate_owner('acme','a')
    record=(await service.list(who))[0]
    clock[0]+=86401
    outputs.append(candidate(followup_id=record['id']))
    assert await service.evaluate_owner('acme','a')
    assert any(row['ref']=='message:1' for row in sampled[-1]['evidence'])
    assert len(await control.reminder_store.notifications('acme','a',0,100))==2


async def test_completed_item_cannot_become_a_new_followup(business):
    """即使模型建议错误，服务端也拒绝已完成待办的重新催办。"""
    service,workspace,control,_,who,_,outputs,_=business
    item=await workspace.create(who,'todo',{'title':'发报价','status':'done'},'already-done')
    await workspace.append_message(who,'user','发报价','x:user')
    outputs.append(candidate(item_id=item['id'],evidence_refs=['message:1',f"item:{item['id']}:1"]))
    assert await service.evaluate_owner('acme','a') is False
    assert await control.reminder_store.notifications('acme','a',0,100)==[]


async def test_same_message_multiple_commitments_survive(business):
    """一条消息的多个独立承诺分别保留，不按整条消息合成一项。"""
    service,workspace,_,_,who,_,outputs,_=business
    await workspace.append_message(who,'user','下午发报价和合同','multi:user')
    first=candidate()['followups'][0]
    second=dict(first,title='发合同',commitment_index=1)
    outputs.append({'followups':[first,second]})
    assert await service.evaluate_owner('acme','a')
    assert len(await service.list(who))==2


async def test_postponed_rule_task_has_single_attention_at_new_time(business):
    """用户安排长期等待后，到新时间规则与业务跟进不能双发。"""
    service,workspace,control,_,who,clock,_,_=business
    await workspace.create(who,'todo',{'title':'交报告','due_at':clock[0]-1},'later-task')
    await service.scan_owner('acme','a')
    original=(await service.list(who))[0]
    due=clock[0]+2*86400
    waiting=await service.act(who,original['id'],ActionInput(request_id='later-two-days',
        version=original['version'],action='postpone',due_at=due))
    assert waiting['status']=='waiting'
    before=len(await control.reminder_store.notifications('acme','a',0,100))
    clock[0]=due+1
    await service.scan_owner('acme','a')
    assert len(await control.reminder_store.notifications('acme','a',0,100))==before+1


async def test_business_commitment_source_survives_incremental_cursor(business):
    """业务会话审计消费后，周期核实仍恢复本人原承诺。"""
    from xuanpu_server.controlplane.models import AuditRecord, SessionRecord

    service,_,control,_,who,clock,outputs,sampled=business
    await control.create_session(SessionRecord('quote-session','acme','support','1.0.0','main',
        clock[0],clock[0],'user:a',user_id='a'))
    await control.append_audit([AuditRecord('acme','control','message_sent',clock[0],
        'user:a',session_id='quote-session',user_id='a',data={'text':'下午把报价发给客户'})])
    outputs.append(candidate(evidence_refs=['business:quote-session:1']))
    assert await service.evaluate_owner('acme','a')
    record=(await service.list(who))[0]
    clock[0]+=86401
    outputs.append(candidate(evidence_refs=record['evidence_refs'],followup_id=record['id']))
    assert await service.evaluate_owner('acme','a')
    assert any(row['ref']=='business:quote-session:1' for row in sampled[-1]['evidence'])


async def test_review_rotates_more_than_twenty_existing_commitments(business):
    """超过一轮证据容量的旧承诺在后续周期得到核实，不永久饥饿。"""
    from copy import deepcopy

    service,workspace,_,_,who,clock,outputs,sampled=business
    await workspace.append_message(who,'user','下午发报价','rotate:1')
    outputs.append(candidate())
    await service.evaluate_owner('acme','a')
    original=(await service.list(who))[0]
    for index in range(2,26):
        await workspace.append_message(who,'user',f'下午发第{index}份报价',f'rotate:{index}')
    async with service.transaction('acme','a') as state:
        state.scan_state['message_cursor']=25
        for index in range(2,26):
            row=deepcopy(original)
            row.update(id=f'rotate-{index:02}',stable_key=f'rotate-{index:02}',evidence_refs=[f'message:{index}'],next_review_at=clock[0])
            state.followups[row['id']]=row
    clock[0]+=86401
    outputs.append({'followups':[]})
    assert await service.evaluate_owner('acme','a')
    first={row['ref'] for row in sampled[-1]['evidence']}
    clock[0]+=1801
    outputs.append({'followups':[]})
    assert await service.evaluate_owner('acme','a')
    second={row['ref'] for row in sampled[-1]['evidence']}
    assert len(first|second)==25


async def test_semantic_refresh_keeps_user_arrangement_and_cancel_link(business):
    """到期后语义复查仍保留用户安排，完成能撤销已积压的原生投递。"""
    from dataclasses import asdict

    from xuanpu_server.reminders.models import ReminderRecord

    service,workspace,control,_,who,clock,outputs,_=business
    await workspace.append_message(who,'user','下午把报价发给客户','linked:user')
    outputs.append(candidate())
    await service.evaluate_owner('acme','a')
    row=(await service.list(who))[0]
    due=clock[0]+2*86400
    waiting=await service.act(who,row['id'],ActionInput(request_id='linked-later',
        version=row['version'],action='postpone',due_at=due))
    native=ReminderRecord('linked-native','acme','a','跟进报价',due,'UTC','once',
        ('inbox','feishu'),'active',1,'linked-native',clock[0],clock[0],'fp')
    async with service.transaction('acme','a') as state:
        state.pending_reminders.append(asdict(native))
        state.followups[row['id']]['reminder_id']=native.reminder_id
    clock[0]=due+1
    notification=await control.reminder_store.fire(native,clock[0])
    assert notification is not None
    await service.scan_owner('acme','a')
    outputs.append(candidate(followup_id=row['id']))
    assert await service.evaluate_owner('acme','a')
    latest=await service.get(who,row['id'])
    assert latest['reminder_id']==native.reminder_id and latest['due_at']==due
    assert latest['handled_at']==waiting['handled_at']
    await service.act(who,row['id'],ActionInput(request_id='linked-complete',
        version=latest['version'],action='complete'))
    assert not await control.reminder_store.notification_current(notification)


async def test_one_todo_cannot_be_split_by_model_commitment_index(business):
    """待办身份由平台确定，模型序号不得拆成多条关注和绕过去重。"""
    service,workspace,control,_,who,_,outputs,_=business
    item=await workspace.create(who,'todo',{'title':'发报价'},'unique-item')
    first=candidate(item_id=item['id'],evidence_refs=[f"item:{item['id']}:1"])['followups'][0]
    outputs.append({'followups':[first,dict(first,commitment_index=1)]})
    assert await service.evaluate_owner('acme','a')
    assert len(await service.list(who))==1
    assert len(await control.reminder_store.notifications('acme','a',0,100))==1


async def test_large_item_and_item_backlog_eventually_read(business):
    """长描述不阻断后续记录，事项独立游标最终读到页尾。"""
    service,workspace,_,_,who,clock,outputs,sampled=business
    items=[]
    for index in range(31):
        items.append(await workspace.create(who,'todo',{'title':f'第{index}份报价',
            'description':'中'*20000 if index==0 else ''},f'item-page:{index}'))
    seen=set()
    for _ in range(12):
        outputs.append({'followups':[]})
        assert await service.evaluate_owner('acme','a')
        seen.update(row['id'] for row in sampled[-1]['evidence'] if row['kind']=='item')
        clock[0]+=1801
    assert seen=={item['id'] for item in items}


async def test_restore_business_reads_latest_source_session_outside_scan_page(business):
    """续扫另一会话时，原承诺关联会话的新完成消息仍必须送入复查。"""
    from xuanpu_server.controlplane.models import AuditRecord, SessionRecord

    service,_,control,_,who,clock,outputs,sampled=business
    for index in range(6):
        await control.create_session(SessionRecord(f's{index}','acme','support','1.0.0','main',
            clock[0],clock[0],'user:a',user_id='a'))
    await control.append_audit([AuditRecord('acme','control','message_sent',clock[0],
        'user:a',session_id='s0',user_id='a',data={'text':'下午发报价'})])
    outputs.append(candidate(evidence_refs=['business:s0:1']))
    assert await service.evaluate_owner('acme','a')
    await control.append_audit([AuditRecord('acme','control','message_sent',clock[0]+1,
        'user:a',session_id='s0',user_id='a',data={'text':'报价已完成，不用再跟进'})])
    clock[0]+=86401
    outputs.append({'followups':[]})
    assert await service.evaluate_owner('acme','a')
    assert any('报价已完成' in row.get('text','') for row in sampled[-1]['evidence'])
    clock[0]+=86401
    outputs.append({'followups':[]})
    assert await service.evaluate_owner('acme','a')
    assert any('报价已完成' in row.get('text','') for row in sampled[-1]['evidence'])


@pytest.mark.parametrize('character',['中','😀'])
async def test_long_message_tail_is_read_before_record_cursor_advances(business,character):
    """长消息尾部承诺最终入模，未读完不能把整条消息记为已消费。"""
    service,workspace,_,_,who,clock,outputs,sampled=business
    await workspace.append_message(who,'user',character*5000+'明天把终稿发给客户','long-tail:user')
    observed=[]
    for index in range(6):
        outputs.append({'followups':[]})
        assert await service.evaluate_owner('acme','a')
        observed.extend(row for row in sampled[-1]['evidence'] if row['ref'].startswith('message:1'))
        async with service.transaction('acme','a') as state:
            assert state.scan_state['message_cursor']==(1 if index==5 else 0)
        clock[0]+=1801
    assert any('终稿发给客户' in row['text'] for row in observed)
    assert any(row['ref']=='message:1:5000' for row in observed)

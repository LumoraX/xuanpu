"""组织决定竞争、固定成果与职责歧义，不能从一次回执生成多次批准。"""
from __future__ import annotations

import asyncio

from .test_approval_decision import decide, prepared, request_approval
from .test_handoff_claim import act


async def test_competing_decisions_commit_one_immutable_decision(handoff_http,shared_id):
    """批准与驳回并发只有一个决定，重复同操作号返回原回执。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id)
    created=await request_approval(http,rule,hid,ref); aid=created.json()['id']
    calls=[('approve','approve-race'),('reject','reject-race')]
    replies=await asyncio.gather(*(decide(http['alice'],aid,a,request=r) for a,r in calls))
    assert sorted(r.status_code for r in replies)==[200,409]
    current=(await http['bob'].get('/v1/approval-requests/'+aid)).json()
    assert len(current['decisions'])==1
    index=next(i for i,r in enumerate(replies) if r.status_code==200)
    assert (await decide(http['alice'],aid,calls[index][0],request=calls[index][1])).json()==replies[index].json()
    # 已结束的相同操作号不能换 payload。
    assert (await decide(http['alice'],aid,calls[index][0],request=calls[index][1],reason='换内容')).status_code==409


async def test_bad_sha_and_second_pending_do_not_create_approval(handoff_http,shared_id):
    """SHA不是客户端声称即信任，同一交接不能并行积累待决请求。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id)
    denied=await request_approval(http,rule,hid,{**ref,'sha256':'0'*64},'wrong-result-sha')
    assert denied.status_code==409,denied.text
    assert (await request_approval(http,rule,hid,ref)).status_code==201
    assert (await request_approval(http,rule,hid,ref,'second-pending')).status_code==409
    assert (await http['bob'].get('/v1/approval-requests')).json()['total']==1


async def test_input_resubmission_supersedes_pending_and_old_decision_fails(handoff_http,shared_id):
    """补充重新提交不复用旧输入的组织批准，历史保留。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id,('usr_tenant_admin',))
    created=await request_approval(http,rule,hid,ref);aid=created.json()['id']
    assert (await act(http['bob'],hid,'request-information',3,'approval-needs-input',reason='需补充')).status_code==200
    source=(await http['alice'].get('/v1/shared-matters/'+shared_id)).json()
    changed=await act(http['alice'],hid,'provide-information',4,'approval-new-input',reason='更新说明',
        shared_version=source['version'],summary='新的明确输入')
    assert changed.status_code==200,changed.text
    assert (await decide(http['tenant_admin'],aid,'approve')).status_code==409
    assert (await http['bob'].get('/v1/approval-requests/'+aid)).json()['status']=='superseded'
    assert (await http['tenant_admin'].get('/v1/shared-matters/'+shared_id)).status_code==404


async def test_unit_role_requires_unique_current_approver(handoff_http,shared_id,organization_ids):
    """可选部门审批多人匹配时不能猜一位，更不能默认自动通过。"""
    http=handoff_http;admin=http['tenant_admin'];unit=organization_ids['a']
    for user in ('usr_alice','usr_tenant_admin'):
        assert (await admin.post('/v1/admin/organization/memberships',json={
            'request_id':'ambiguous-member-'+user,'user_id':user,'unit_id':unit})).status_code==201
        assert (await admin.post('/v1/admin/organization/roles',json={
            'request_id':'ambiguous-role-'+user,'user_id':user,'scope_id':unit,'role':'unit_approver'})).status_code==201
    rule,hid,ref=await prepared(http,shared_id)
    # 冻结原交接策略第1版不应由修订偷换，因此另建明确引用单位规则的交接。
    from .test_handoff_claim import submit
    configured=await admin.post('/v1/admin/approval-policies',json={'request_id':'unit-policy','confirmed':True,
        'name':'部门审批','steps':[{'selector':'unit_role','unit_id':unit,'role':'unit_approver'}]})
    current=(await http['alice'].get('/v1/shared-matters/'+shared_id+'/materials')).json()
    revision=current['items'][0]['revisions'][0]['id']
    handoff=await submit(http,shared_id,'unit-approval-handoff',policy_id=configured.json()['id'],material_revision_ids=[revision])
    identifier=handoff.json()['id']
    assert (await act(http['bob'],identifier,'accept',1,'unit-approval-accept')).status_code==200
    assert (await act(http['bob'],identifier,'start',2,'unit-approval-start')).status_code==200
    requested=await request_approval(http,configured.json()['id'],identifier,ref,'unit-ambiguous-request')
    assert requested.status_code==409,requested.text


async def test_new_request_cannot_silently_use_superseded_policy_revision(handoff_http,shared_id):
    """已有请求冻结旧策略，新申请遇规则更新须先由owner重新明确交接输入。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id)
    changed=await http['tenant_admin'].patch('/v1/admin/approval-policies/'+rule,json={
        'request_id':'policy-new-before-request','confirmed':True,'expected_version':1,'name':'新规则',
        'steps':[{'selector':'user','user_id':'usr_tenant_admin'}]})
    assert changed.status_code==200
    denied=await request_approval(http,rule,hid,ref,'must-reconfirm-policy')
    assert denied.status_code==409,denied.text
    assert (await http['bob'].get('/v1/approval-requests')).json()['total']==0


async def test_unit_approver_added_between_candidate_read_and_lock_is_not_ignored(handoff_http,shared_id,organization_ids,other_employee,r3_world,monkeypatch):
    """读取候选后另一名合法审批人到任，必须冲突重核，不能忽略新候选。"""
    from contextlib import asynccontextmanager

    from .test_handoff_claim import submit
    http=handoff_http;admin=http['tenant_admin'];unit=organization_ids['a']
    for user in ('usr_alice','usr_carol'):
        assert (await admin.post('/v1/admin/organization/memberships',json={
            'request_id':'race-member-'+user,'user_id':user,'unit_id':unit})).status_code==201
    assert (await admin.post('/v1/admin/organization/roles',json={'request_id':'race-role-first',
        'user_id':'usr_alice','scope_id':unit,'role':'unit_approver'})).status_code==201
    _,_,ref=await prepared(http,shared_id)
    rule=await admin.post('/v1/admin/approval-policies',json={'request_id':'race-unit-policy','confirmed':True,
        'name':'单位规则','steps':[{'selector':'unit_role','unit_id':unit,'role':'unit_approver'}]})
    rows=(await http['alice'].get('/v1/shared-matters/'+shared_id+'/materials')).json()['items']
    created=await submit(http,shared_id,'race-unit-handoff',policy_id=rule.json()['id'],material_revision_ids=[rows[0]['revisions'][0]['id']])
    hid=created.json()['id']
    assert (await act(http['bob'],hid,'accept',1,'race-unit-accept')).status_code==200
    assert (await act(http['bob'],hid,'start',2,'race-unit-start')).status_code==200
    service=r3_world.platform.handoffs;original=service.transaction;injected=False
    @asynccontextmanager
    async def transaction(*args,**kwargs):
        """只在首个预读事务退出后增加真实职责，保持正式锁序。"""
        nonlocal injected
        async with original(*args,**kwargs) as pair:
            yield pair
        if not injected:
            injected=True
            assigned=await admin.post('/v1/admin/organization/roles',json={'request_id':'race-role-second',
                'user_id':'usr_carol','scope_id':unit,'role':'unit_approver'})
            assert assigned.status_code==201,assigned.text
    monkeypatch.setattr(service,'transaction',transaction)
    response=await request_approval(http,rule.json()['id'],hid,ref,'candidate-race-request')
    assert response.status_code==409,response.text
    assert (await http['bob'].get('/v1/approval-requests')).json()['total']==0

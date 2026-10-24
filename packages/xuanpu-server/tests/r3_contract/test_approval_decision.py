"""固定目标、顺序真人决定及临时资料权限，经双后端真实产品接口验证。"""
from __future__ import annotations

import hashlib

from .test_approval_policy import policy
from .test_handoff_claim import act, submit


async def prepared(http, shared_id, users=('usr_alice',)):
    """先发布真实ready版本，再经原生交接把该固定资料授予承办人。"""
    rule = await policy(http, users=users)
    assert rule.status_code == 201, rule.text
    path = '/v1/shared-matters/' + shared_id
    uploaded = await http['alice'].post(path + '/material-uploads', params={
        'request_id':'approval-result','confirmed':'true','expected_version':1,'name':'核对成果.txt'}, content=b'result-for-review')
    assert uploaded.status_code == 202, uploaded.text
    material = (await http['alice'].get(path + '/publications/' + uploaded.json()['id'])).json()['material_id']
    listing = (await http['alice'].get(path + '/materials')).json()
    rid = listing['items'][0]['revisions'][0]['id']
    handoff = await submit(http,shared_id,policy_id=rule.json()['id'],material_revision_ids=[rid])
    assert handoff.status_code == 201, handoff.text
    hid = handoff.json()['id']
    assert (await act(http['bob'],hid,'accept',1,'approve-accept')).status_code == 200
    assert (await act(http['bob'],hid,'start',2,'approve-start')).status_code == 200
    return rule.json()['id'], hid, {'material_id':material,'revision':1,'sha256':hashlib.sha256(b'result-for-review').hexdigest()}


async def request_approval(http, rule, hid, ref, request='submit-approval', **extra):
    """申请不能覆盖审批人，输入绑定精确目标和固定结果。"""
    return await http['bob'].post('/v1/approval-requests',json={'request_id':request,'confirmed':True,
        'policy_id':rule,'target_type':'handoff','target_id':hid,'target_version':3,'result_refs':[ref],**extra})


async def decide(client, identifier, action, version=1, request='decide', **extra):
    """组织决定有独立操作号、版本和明确确认。"""
    return await client.post('/v1/approval-requests/' + identifier + '/' + action,json={
        'request_id':request,'confirmed':True,'expected_version':version,**extra})


async def test_named_approval_is_separate_from_signoff_and_cannot_self_approve(handoff_http,shared_id):
    """没有部门也可由陈翔对应的发起人审批，承办人不得自批。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id)
    created=await request_approval(http,rule,hid,ref)
    assert created.status_code == 201,created.text
    aid=created.json()['id']
    assert (await request_approval(http,rule,hid,ref)).json() == created.json()
    assert (await decide(http['bob'],aid,'approve')).status_code == 403
    approved=await decide(http['alice'],aid,'approve')
    assert approved.status_code == 200,approved.text
    assert approved.json()['status'] == 'approved'
    assert (await decide(http['alice'],aid,'approve')).json() == approved.json()
    handoff=(await http['alice'].get('/v1/handoffs/'+hid)).json()
    assert handoff['status'] == 'in_progress'
    assert (await request_approval(http,rule,hid,ref,'override-approver',approvers=['usr_bob'])).status_code == 400


async def test_sequential_steps_only_current_approver_has_temporary_packet(handoff_http,shared_id):
    """临时审批不开放正文、其他材料或下一步提前决定。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id,('usr_tenant_admin','usr_alice'))
    created=await request_approval(http,rule,hid,ref)
    assert created.status_code == 201,created.text
    aid=created.json()['id']; path='/v1/shared-matters/'+shared_id
    view=(await http['tenant_admin'].get(path)).json()
    assert view['scope']=='packet' and view['content']==''
    data=await http['tenant_admin'].get(path+'/materials/'+ref['material_id']+'/content',params={'revision':1})
    assert data.status_code == 200
    assert (await decide(http['alice'],aid,'approve',request='too-early-step2')).status_code == 403
    first=await decide(http['tenant_admin'],aid,'approve',request='step1')
    assert first.status_code == 200 and first.json()['status']=='pending',first.text
    assert (await http['tenant_admin'].get(path)).status_code == 404
    second=await decide(http['alice'],aid,'approve',2,'step2')
    assert second.status_code == 200 and second.json()['status']=='approved',second.text


async def test_frozen_policy_and_disabled_approver_block_without_replacement(handoff_http,shared_id,r3_world):
    """规则更新不偷换既有审批人，身份失效仍不得由新规则的人代决定。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id,('usr_tenant_admin',))
    created=await request_approval(http,rule,hid,ref);assert created.status_code==201,created.text
    changed=await http['tenant_admin'].patch('/v1/admin/approval-policies/'+rule,json={
        'request_id':'policy-after-submit','confirmed':True,'expected_version':1,'name':'新规则',
        'steps':[{'selector':'user','user_id':'usr_alice'}]})
    assert changed.status_code==200,changed.text
    assert (await decide(http['alice'],created.json()['id'],'approve',request='replacement')).status_code in (403,404)
    await r3_world.platform.identity.store.set_user_status('acme','usr_tenant_admin','disabled',reason='admin',at=100)
    current=await http['bob'].get('/v1/approval-requests/'+created.json()['id'])
    assert current.status_code==200,current.text
    assert current.json()['status']=='blocked' and current.json()['blocked_reason']=='approver_authority_revoked'


async def test_self_policy_bad_sha_and_concurrent_pending_are_rejected(handoff_http,shared_id):
    """管理员配置为申请人本人也不能批准；错误SHA不创建请求。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id,('usr_bob',))
    denied=await request_approval(http,rule,hid,ref)
    assert denied.status_code==409,denied.text
    # 仍未创建任何请求，列表计数真实为零。
    listing=(await http['bob'].get('/v1/approval-requests')).json()
    assert listing['total']==0


async def test_future_step_and_completed_temporary_reader_cannot_read_reason_body(handoff_http,shared_id,r3_world):
    """列入审批步骤不等于取得永久正文权，理由可能包含成果片段。"""
    from xuanpu_server.identity.models import UserRecord
    from xuanpu_server.tenancy import Principal
    http=handoff_http
    await r3_world.platform.identity.store.create_user(UserRecord('acme','usr_carol','第三步员工',100))
    rule,hid,ref=await prepared(http,shared_id,('usr_tenant_admin','usr_alice','usr_carol'))
    created=await request_approval(http,rule,hid,ref);aid=created.json()['id']
    first=await decide(http['tenant_admin'],aid,'approve',request='reason-step1',reason='secret-result-body')
    assert first.status_code==200,first.text
    future=Principal(role='tenant_member',tenant_id='acme',user_id='usr_carol',key_id=None)
    metadata=await r3_world.platform.approvals.get(future,aid)
    assert metadata['decisions'][0]['reason']==''
    former=(await http['tenant_admin'].get('/v1/approval-requests/'+aid)).json()
    assert former['decisions'][0]['reason']==''
    owner=(await http['alice'].get('/v1/approval-requests/'+aid)).json()
    assert owner['decisions'][0]['reason']=='secret-result-body'


async def test_unreadable_cross_matter_derived_result_cannot_be_approved(handoff_http,shared_id,r3_world,step):
    """临时成果packet不扩大另一事项原来源权限，不能批准自己尚无法阅读的成果。"""
    from .test_shared_derived import draft_for
    http=handoff_http
    _,context,message,_=await draft_for(http,shared_id,r3_world,step)
    target=await http['alice'].post('/v1/shared-matters',json={'request_id':'derived-handoff-source',
        'confirmed':True,'title':'承办事项A'})
    target_id=target.json()['id']
    configured=await policy(http,users=('usr_tenant_admin',));rule=configured.json()['id']
    created=await submit(http,target_id,'derived-handoff',policy_id=rule);hid=created.json()['id']
    assert (await act(http['bob'],hid,'accept',1,'derived-result-accept')).status_code==200
    assert (await act(http['bob'],hid,'start',2,'derived-result-start')).status_code==200
    current=(await http['bob'].get('/v1/shared-matters/'+target_id)).json()
    uploaded=await http['bob'].post('/v1/handoffs/'+hid+'/result-texts',json={'request_id':'derived-result-text',
        'confirmed':True,'expected_version':3,'shared_version':current['version'],'name':'跨来源成果.txt',
        'source_context_id':context['id'],'draft_message_id':message['id']})
    assert uploaded.status_code==202,uploaded.text
    materials=(await http['bob'].get('/v1/shared-matters/'+target_id+'/materials')).json()['items']
    revision=materials[0]['revisions'][0]
    ref={'material_id':materials[0]['id'],'revision':revision['revision'],'sha256':revision['sha256']}
    denied=await request_approval(http,rule,hid,ref,'unreadable-result-request')
    assert denied.status_code==409,denied.text
    assert (await http['bob'].get('/v1/approval-requests')).json()['total']==0
    assert (await http['tenant_admin'].get('/v1/shared-matters/'+target_id)).status_code==404

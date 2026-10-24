"""成果交付与发起人签收分别原生确认，不能把组织批准当作任务完成。"""
from __future__ import annotations

from .test_approval_decision import decide, prepared, request_approval
from .test_handoff_claim import act


async def test_approval_delivery_and_signoff_are_three_separate_records(handoff_http,shared_id):
    """固定成果批准后仍由承办人交付，再由发起人另行签收。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id)
    assert (await act(http['bob'],hid,'deliver',3,'before-org-approval',result_refs=[ref])).status_code==409
    requested=await request_approval(http,rule,hid,ref);aid=requested.json()['id']
    assert (await decide(http['alice'],aid,'approve')).status_code==200
    delivered=await act(http['bob'],hid,'deliver',3,'deliver-fixed',result_refs=[ref],approval_id=aid)
    assert delivered.status_code==200,delivered.text
    assert delivered.json()['status']=='delivered' and delivered.json()['delivery_version']==1
    assert (await act(http['bob'],hid,'sign-off',4,'assignee-sign',delivery_version=1)).status_code==403
    assert (await act(http['alice'],hid,'sign-off',4,'wrong-delivery',delivery_version=2)).status_code==409
    signed=await act(http['alice'],hid,'sign-off',4,'sign-exact',delivery_version=1)
    assert signed.status_code==200 and signed.json()['status']=='signed_off',signed.text
    assert (await act(http['alice'],hid,'sign-off',4,'sign-exact',delivery_version=1)).json()==signed.json()
    final=(await http['alice'].get('/v1/handoffs/'+hid)).json()
    assert len(final['deliveries'])==1
    assert [e['action'] for e in final['history']].count('sign_off')==1


async def test_dispute_reopen_requires_new_approval_and_delivery_version(handoff_http,shared_id):
    """争议保留原交付，重办后的新版本不能套用旧批准或旧签收。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id)
    requested=await request_approval(http,rule,hid,ref);aid=requested.json()['id']
    assert (await decide(http['alice'],aid,'approve')).status_code==200
    assert (await act(http['bob'],hid,'deliver',3,'delivery-before-dispute',result_refs=[ref],approval_id=aid)).status_code==200
    assert (await act(http['alice'],hid,'dispute',4,'dispute-fixed',reason='需要补充说明')).status_code==200
    assert (await act(http['bob'],hid,'reopen',5,'reopen-explicit')).status_code==200
    assert (await act(http['bob'],hid,'deliver',6,'old-approval-on-new-target',result_refs=[ref],approval_id=aid)).status_code==409
    requested=await request_approval(http,rule,hid,ref,'resubmitted-after-dispute',target_version=6)
    assert requested.status_code==201,requested.text
    assert (await decide(http['alice'],requested.json()['id'],'approve',request='approve-reopened')).status_code==200
    delivered=await act(http['bob'],hid,'deliver',6,'deliver-second',result_refs=[ref],approval_id=requested.json()['id'])
    assert delivered.status_code==200 and delivered.json()['delivery_version']==2,delivered.text
    assert (await act(http['alice'],hid,'sign-off',7,'old-signoff',delivery_version=1)).status_code==409
    assert (await act(http['alice'],hid,'sign-off',7,'new-signoff',delivery_version=2)).status_code==200
    assert len((await http['alice'].get('/v1/handoffs/'+hid)).json()['deliveries'])==2


async def test_departed_assignee_does_not_erase_fixed_delivery_or_block_owner_signoff(handoff_http,shared_id,r3_world):
    """已合法交付的固定成果属于事项，原承办停用后owner仍可独立签收。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id)
    requested=await request_approval(http,rule,hid,ref);aid=requested.json()['id']
    assert (await decide(http['alice'],aid,'approve')).status_code==200
    assert (await act(http['bob'],hid,'deliver',3,'deliver-before-departure',result_refs=[ref],approval_id=aid)).status_code==200
    await r3_world.platform.identity.store.set_user_status('acme','usr_bob','disabled',reason='admin',at=100)
    current=(await http['alice'].get('/v1/handoffs/'+hid)).json()
    assert current['blocked_reason'] is None and 'sign_off' in current['allowed_actions'],current
    assert (await act(http['alice'],hid,'sign-off',4,'sign-after-departure',delivery_version=1)).status_code==200


async def test_disputed_work_after_departure_can_be_explicitly_reassigned(handoff_http,shared_id,r3_world,other_employee):
    """争议保留交付史，发起人可明确新输入和新员工，仍需重新接受。"""
    http=handoff_http
    rule,hid,ref=await prepared(http,shared_id)
    requested=await request_approval(http,rule,hid,ref);aid=requested.json()['id']
    assert (await decide(http['alice'],aid,'approve')).status_code==200
    assert (await act(http['bob'],hid,'deliver',3,'disputed-delivery',result_refs=[ref],approval_id=aid)).status_code==200
    assert (await act(http['alice'],hid,'dispute',4,'explicit-dispute',reason='需要重办')).status_code==200
    await r3_world.platform.identity.store.set_user_status('acme','usr_bob','disabled',reason='admin',at=100)
    current=(await http['alice'].get('/v1/handoffs/'+hid)).json()
    assert 'transfer' in current['allowed_actions']
    source=(await http['alice'].get('/v1/shared-matters/'+shared_id)).json()
    moved=await act(http['alice'],hid,'transfer',current['version'],'dispute-new-assignee',
        reason='重新安排承办',user_id='usr_carol',shared_version=source['version'],summary='重办核对')
    assert moved.status_code==200 and moved.json()['status']=='submitted',moved.text
    assert (await act(other_employee,hid,'accept',moved.json()['version'],'carol-reaccept')).status_code==200
    assert len((await http['alice'].get('/v1/handoffs/'+hid)).json()['deliveries'])==1

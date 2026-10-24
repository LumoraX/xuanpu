"""协作站内动态与统一入口共用当前本人身份，不保存业务标题或理由正文。"""
from __future__ import annotations

from .test_handoff_claim import act, submit


async def test_collaboration_notifications_are_atomic_scoped_and_replayable(handoff_http,shared_id):
    """提交一次生成一份到人通知，独立总数、未读数和稳定标读。"""
    http=handoff_http
    created=await submit(http,shared_id,summary='不应出现在通知正文的敏感说明')
    assert created.status_code==201
    assert (await submit(http,shared_id,summary='不应出现在通知正文的敏感说明')).json()==created.json()
    page=(await http['bob'].get('/v1/collaboration-notifications')).json()
    assert page['total']==1 and page['unread_total']==1
    assert '敏感说明' not in str(page)
    note=page['items'][0]
    assert (await http['tenant_admin'].get('/v1/collaboration-notifications')).json()['total']==0
    body={'request_id':'read-collaboration','confirmed':True,'expected_version':note['version']}
    result=await http['bob'].post('/v1/collaboration-notifications/'+note['id']+'/read',json=body)
    assert result.status_code==200,result.text
    assert (await http['bob'].post('/v1/collaboration-notifications/'+note['id']+'/read',json=body)).json()==result.json()
    unified=(await http['bob'].get('/v1/notifications')).json()
    assert unified['collaboration']['total']==1 and unified['collaboration']['unread_total']==0
    assert (await http['alice'].post('/v1/collaboration-notifications/'+note['id']+'/read',json=body)).status_code==404
    assert (await act(http['bob'],created.json()['id'],'accept',1,'note-accept')).status_code==200
    assert (await http['alice'].get('/v1/collaboration-notifications')).json()['unread_total']==1


async def test_collaboration_permission_hint_changes_on_source_loss(handoff_http,shared_id,r3_world):
    """即使尚未接受且没有共享grant，收到的交接仍即时发现来源身份失效。"""
    http=handoff_http
    assert (await submit(http,shared_id)).status_code==201
    before=(await http['bob'].get('/v1/collaboration-notifications')).json()['permission_revision']
    await r3_world.platform.identity.store.set_user_status('acme','usr_alice','disabled',reason='admin',at=100)
    after=(await http['bob'].get('/v1/collaboration-notifications')).json()['permission_revision']
    assert before!=after


async def test_finished_approval_hint_tracks_current_reason_access(handoff_http, shared_id):
    """审批完成后独立阅读授权撤销，仍须提示清除已经渲染的理由。"""
    from .test_approval_decision import decide, prepared, request_approval
    http = handoff_http
    rule, hid, ref = await prepared(http, shared_id, ('usr_tenant_admin',))
    path = '/v1/shared-matters/' + shared_id
    source = (await http['alice'].get(path)).json()
    grant = await http['alice'].post(path + '/members', json={
        'request_id': 'independent-approver-reader', 'confirmed': True, 'expected_version': source['version'],
        'subject_kind': 'user', 'subject_id': 'usr_tenant_admin', 'role': 'viewer'})
    assert grant.status_code == 201, grant.text
    created = await request_approval(http, rule, hid, ref)
    assert created.status_code == 201, created.text
    aid = created.json()['id']
    assert (await decide(http['tenant_admin'], aid, 'approve', reason='secret-result-body')).status_code == 200
    detail = (await http['tenant_admin'].get('/v1/approval-requests/' + aid)).json()
    assert detail['decisions'][0]['reason'] == 'secret-result-body'
    before = (await http['tenant_admin'].get('/v1/collaboration-notifications')).json()
    source = (await http['alice'].get(path)).json()
    revoked = await http['alice'].post(path + '/members/' + grant.json()['id'] + '/revoke', json={
        'request_id': 'remove-independent-reader', 'confirmed': True, 'expected_version': source['version']})
    assert revoked.status_code == 200, revoked.text
    after = (await http['tenant_admin'].get('/v1/collaboration-notifications')).json()
    assert before['permission_revision'] != after['permission_revision']
    assert before['visibility_revision'] != after['visibility_revision']
    detail = (await http['tenant_admin'].get('/v1/approval-requests/' + aid)).json()
    assert detail['decisions'][0]['reason'] == '' and detail['can_read_results'] is False

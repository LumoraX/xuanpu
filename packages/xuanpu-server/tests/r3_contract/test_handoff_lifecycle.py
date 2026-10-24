"""交接的取消、补充、重新安排和即时撤权，不靠原进程内存修正。"""
from __future__ import annotations

from .test_handoff_claim import act, submit
from .test_shared_context import context_for


async def test_information_resubmission_revokes_old_packet_and_requires_reaccept(handoff_http, shared_id):
    """补充是新的明确输入，承办授权与旧阅读不会自动沿用。"""
    http = handoff_http
    created = await submit(http, shared_id, include_content=True)
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    assert (await act(http['bob'], identifier, 'accept', 1, 'info-accept')).status_code == 200
    visible = (await http['bob'].get('/v1/shared-matters/' + shared_id)).json()
    context = await context_for(http['bob'], shared_id, 'handoff-old-read', visible['version'])
    assert context.status_code == 201
    request = await act(http['bob'], identifier, 'request-information', 2, 'info-request', reason='缺少核对说明')
    assert request.status_code == 200, request.text
    source = (await http['alice'].get('/v1/shared-matters/' + shared_id)).json()
    result = await act(http['alice'], identifier, 'provide-information', 3, 'info-provide', reason='已核对说明',
        shared_version=source['version'], summary='重新明确的核对说明', include_content=True)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'submitted' and result.json()['assignee_id'] is None
    assert (await http['bob'].get('/v1/shared-matters/' + shared_id)).status_code == 404
    assert (await http['bob'].get('/v1/shared-contexts/' + context.json()['id'] + '/read')).status_code == 403
    assert (await act(http['bob'], identifier, 'accept', 4, 'info-reaccept')).status_code == 200
    assert (await http['bob'].get('/v1/shared-contexts/' + context.json()['id'] + '/read')).status_code == 403


async def test_request_cancel_requires_assignee_confirmation(handoff_http, shared_id):
    """接受后不能由发起人删除责任，等待取消期间禁止继续处理。"""
    http = handoff_http
    created = await submit(http, shared_id)
    identifier = created.json()['id']
    assert (await act(http['bob'], identifier, 'accept', 1, 'cancel-accept')).status_code == 200
    assert (await act(http['alice'], identifier, 'cancel', 2, 'cancel-too-late')).status_code == 409
    asked = await act(http['alice'], identifier, 'request-cancel', 2, 'request-cancel', reason='暂不继续')
    assert asked.status_code == 200 and asked.json()['status'] == 'accepted'
    assert (await act(http['bob'], identifier, 'start', 3, 'start-while-cancel')).status_code == 409
    result = await act(http['bob'], identifier, 'confirm-cancel', 3, 'confirm-cancel')
    assert result.status_code == 200 and result.json()['status'] == 'cancelled'
    assert (await http['bob'].get('/v1/shared-matters/' + shared_id)).status_code == 404


async def test_received_list_checks_revoked_source_before_title(handoff_http, shared_id, r3_world):
    """列表即时核对源员工身份，不先返回旧标题再依赖异步清理。"""
    http = handoff_http
    created = await submit(http, shared_id)
    assert created.status_code == 201
    await r3_world.platform.identity.store.set_user_status('acme', 'usr_alice', 'disabled', reason='admin', at=100)
    listed = (await http['bob'].get('/v1/handoffs', params={'view':'received'})).json()
    assert listed['items'][0]['blocked_reason'] == 'source_access_revoked'
    assert listed['items'][0]['title'] == '交接待重新安排'


async def test_purpose_packet_cannot_bypass_revoked_unit_role(handoff_http, shared_id, organization_ids, r3_world, step):
    """先通过共享入口读取，不依赖交接get或后台生命周期先把授权清理掉。"""
    import json
    http = handoff_http
    admin, unit = http['tenant_admin'], organization_ids['a']
    membership = await admin.post('/v1/admin/organization/memberships', json={
        'request_id':'purpose-member','user_id':'usr_bob','unit_id':unit})
    assert membership.status_code == 201
    role = await admin.post('/v1/admin/organization/roles', json={
        'request_id':'purpose-role','user_id':'usr_bob','scope_id':unit,'role':'unit_operator'})
    assert role.status_code == 201
    created = await submit(http, shared_id, target_kind='unit', target_id=unit, include_content=True)
    assert created.status_code == 201, created.text
    assert (await act(http['bob'], created.json()['id'], 'accept', 1, 'purpose-accept')).status_code == 200
    visible = (await http['bob'].get('/v1/shared-matters/' + shared_id)).json()
    context = await context_for(http['bob'], shared_id, 'purpose-context', visible['version'])
    assert context.status_code == 201
    revoked = await admin.patch('/v1/admin/organization/roles/' + role.json()['id'], json={
        'request_id':'purpose-revoke','expected_version':1,'status':'revoked'})
    assert revoked.status_code == 200
    assert (await http['bob'].get('/v1/shared-matters/' + shared_id)).status_code == 404
    assert (await http['bob'].get('/v1/shared-contexts/' + context.json()['id'] + '/read')).status_code == 403
    r3_world.models.script(step(text=json.dumps({'message':'不该送出'})))
    before = len(r3_world.models.requests)
    routed = await http['bob'].post('/v1/assistant/route', json={'request_id':'purpose-never-model',
        'text':'继续阅读','shared_context_id':context.json()['id']})
    assert routed.status_code == 403 and len(r3_world.models.requests) == before


async def test_unrelated_disabled_operator_does_not_block_target_unit(handoff_http, shared_id, organization_ids, r3_world):
    """停用员工异步职责尚未收尾，不阻塞无关部门有资格的接收人。"""
    http = handoff_http
    admin = http['tenant_admin']
    from xuanpu_server.identity.models import UserRecord
    await r3_world.platform.identity.store.create_user(UserRecord(
        'acme', 'usr_carol', '另一位员工', 100))
    for user, unit in [('usr_bob',organization_ids['a']),('usr_carol',organization_ids['b'])]:
        assert (await admin.post('/v1/admin/organization/memberships',json={
            'request_id':'unrelated-member-'+user,'user_id':user,'unit_id':unit})).status_code == 201
        assert (await admin.post('/v1/admin/organization/roles',json={
            'request_id':'unrelated-role-'+user,'user_id':user,'scope_id':unit,'role':'unit_operator'})).status_code == 201
    await r3_world.platform.identity.store.set_user_status('acme','usr_carol','disabled',reason='admin',at=100)
    created = await submit(http, shared_id, target_kind='unit',target_id=organization_ids['a'])
    assert created.status_code == 201, created.text


async def test_actual_owner_grant_is_source_instead_of_incidental_unit_grant(handoff_http, shared_id, organization_ids):
    """当前owner的独立权属不因无关部门授权失效而被错误阻塞。"""
    http = handoff_http
    admin, path = http['tenant_admin'], '/v1/shared-matters/' + shared_id
    member = await admin.post('/v1/admin/organization/memberships', json={
        'request_id':'owner-member','user_id':'usr_bob','unit_id':organization_ids['a']})
    assert member.status_code == 201
    grant = await http['alice'].post(path + '/members',json={'request_id':'owner-incidental',
        'confirmed':True,'expected_version':1,'subject_kind':'unit','subject_id':organization_ids['a'],'role':'viewer'})
    assert grant.status_code == 201
    transferred = await http['alice'].post(path + '/transfer-owner',json={
        'request_id':'owner-transfer','confirmed':True,'expected_version':2,'user_id':'usr_bob'})
    assert transferred.status_code == 200
    current = (await http['bob'].get(path)).json()
    created = await http['bob'].post('/v1/handoffs',json={'request_id':'owner-handoff','confirmed':True,
        'expected_version':current['version'],'shared_id':shared_id,'target_kind':'user','target_id':'usr_alice',
        'title':'owner明确交接','summary':'来源应为owner','include_content':True})
    assert created.status_code == 201, created.text
    revoked = await admin.patch('/v1/admin/organization/memberships/' + member.json()['id'],json={
        'request_id':'owner-drop-unit','expected_version':1,'status':'revoked'})
    assert revoked.status_code == 200
    assert (await http['bob'].get(path)).status_code == 200
    handoff = (await http['bob'].get('/v1/handoffs/' + created.json()['id'])).json()
    assert handoff['blocked_reason'] is None


async def test_independent_full_grant_keeps_original_purpose_context(handoff_http, shared_id, organization_ids):
    """新增独立授权不撤销仍有效的原用途足迹。"""
    http = handoff_http
    created = await submit(http, shared_id, include_content=True)
    assert (await act(http['bob'], created.json()['id'], 'accept', 1, 'full-after-packet')).status_code == 200
    path = '/v1/shared-matters/' + shared_id
    visible = (await http['bob'].get(path)).json()
    context = await context_for(http['bob'], shared_id, 'full-after-context', visible['version'])
    assert context.status_code == 201
    assert (await http['tenant_admin'].post('/v1/admin/organization/memberships', json={
        'request_id':'full-independent-member','user_id':'usr_bob','unit_id':organization_ids['a']})).status_code == 201
    current = (await http['alice'].get(path)).json()
    granted = await http['alice'].post(path + '/members', json={'request_id':'independent-full',
        'confirmed':True,'expected_version':current['version'],'subject_kind':'unit','subject_id':organization_ids['a'],'role':'viewer'})
    assert granted.status_code == 201, granted.text
    assert (await http['bob'].get('/v1/shared-contexts/' + context.json()['id'] + '/read')).status_code == 200


async def test_source_employee_disabled_closes_purpose_shared_entry(handoff_http, shared_id, r3_world):
    """源owner身份停用立即拒绝共享正文，不等待交接get先标记阻塞。"""
    http = handoff_http
    created = await submit(http, shared_id, include_content=True)
    assert (await act(http['bob'], created.json()['id'], 'accept', 1, 'source-disabled')).status_code == 200
    assert (await http['bob'].get('/v1/shared-matters/' + shared_id)).status_code == 200
    await r3_world.platform.identity.store.set_user_status('acme','usr_alice','disabled',reason='admin',at=100)
    assert (await http['bob'].get('/v1/shared-matters/' + shared_id)).status_code == 404


async def test_lock_expansion_stops_at_actual_source_not_other_employee_handoffs(handoff_http, shared_id, r3_world):
    """锁集合算法的独立反例：互不相干的资料不构成递归来源链。"""
    from xuanpu_server.collaboration.locks import purpose_participants
    http = handoff_http
    created = await submit(http, shared_id)
    assert (await act(http['bob'], created.json()['id'], 'accept', 1, 'lock-base')).status_code == 200
    async with r3_world.platform.handoffs.store.transaction('acme', participants=('usr_alice','usr_bob')) as tx:
        state = tx.state.model_copy(deep=True)
    source = state.collaboration.handoffs[created.json()['id']]
    grant = state.shared.grants[source.assignee_grant_id]
    # 这里只测试锁集合计算，不把手造业务记录当成服务集成证据。
    state.collaboration.handoffs.clear()
    state.shared.grants.clear()
    for index in range(20):
        identifier = 'lock-handoff-' + str(index)
        recipient, requester = 'lock-user-' + str(index), 'lock-user-' + str(index + 1)
        state.collaboration.handoffs[identifier] = source.model_copy(update={
            'id':identifier,'requester_id':requester,'assignee_id':recipient,'target_id':recipient})
        gid = 'lock-grant-' + str(index)
        state.shared.grants[gid] = grant.model_copy(update={'id':gid,'handoff_id':identifier,'subject_id':recipient})
    assert purpose_participants(state, ('lock-user-0',)) == ('lock-user-0', 'lock-user-1')

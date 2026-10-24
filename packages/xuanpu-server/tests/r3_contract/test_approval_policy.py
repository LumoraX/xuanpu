"""到人审批策略只由当前有权管理员原生配置，不要求部门。"""
from __future__ import annotations


async def policy(http, request='approval-policy', users=('usr_alice',)):
    """配置一到三名明确员工，不从企微可见范围推断职责。"""
    return await http['tenant_admin'].post('/v1/admin/approval-policies', json={
        'request_id':request,'confirmed':True,'name':'固定成果核对',
        'steps':[{'selector':'user','user_id':u} for u in users]})


async def test_named_policy_without_department_is_admin_only_and_replayable(handoff_http):
    """规则配置独立于交接承办权限，重放不增加修订。"""
    http = handoff_http
    body = {'request_id':'not-policy-admin','confirmed':True,'name':'越权规则',
        'steps':[{'selector':'user','user_id':'usr_bob'}]}
    assert (await http['bob'].post('/v1/admin/approval-policies', json=body)).status_code == 403
    created = await policy(http)
    assert created.status_code == 201, created.text
    assert (await policy(http)).json() == created.json()
    listing = (await http['tenant_admin'].get('/v1/admin/approval-policies')).json()
    assert listing['total'] == 1 and listing['items'][0]['revisions'][0]['steps'][0]['user_epoch'] == 1
    assert (await http['machine'].post('/v1/admin/approval-policies', json=body)).status_code == 403


async def test_policy_revision_is_append_only_and_identity_bound(handoff_http, r3_world):
    """修订不会覆盖既有步骤；停用员工不可成为新规则的审批人。"""
    http = handoff_http
    created = await policy(http)
    identifier = created.json()['id']
    edited = await http['tenant_admin'].patch('/v1/admin/approval-policies/' + identifier,json={
        'request_id':'policy-v2','confirmed':True,'expected_version':1,'name':'第二版',
        'steps':[{'selector':'user','user_id':'usr_bob'}]})
    assert edited.status_code == 200, edited.text
    assert len(edited.json()['revisions']) == 2 and edited.json()['revisions'][0]['name'] == '固定成果核对'
    await r3_world.platform.identity.store.set_user_status('acme','usr_bob','disabled',reason='admin',at=100)
    assert (await policy(http,'policy-disabled-user',('usr_bob',))).status_code in (403,409)


async def test_tenant_admin_machine_can_configure_without_fake_employee(handoff_http,r3_world):
    """平台管理员预配规则不必把普通员工升为管理员，普通机器key仍无权。"""
    import httpx
    from xuanpu_server.api.app import create_app
    issued=await r3_world.platform.issue_key(r3_world.admin,tenant_id='acme',name='规则配置验收',role='tenant_admin')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(r3_world.platform)),
            base_url='https://office.test',headers={'Authorization':'Bearer '+issued.key}) as client:
        created=await client.post('/v1/admin/approval-policies',json={'request_id':'admin-machine-policy','confirmed':True,
            'name':'发起人明确核对','steps':[{'selector':'user','user_id':'usr_alice'}]})
        assert created.status_code==201,created.text
        assert (await client.get('/v1/admin/approval-policies')).json()['total']==1
        assert (await client.get('/v1/approval-requests')).status_code==403

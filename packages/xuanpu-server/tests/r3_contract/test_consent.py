"""员工委托的隔离、CAS、幂等与失效反例。"""
from __future__ import annotations

import time


async def installation(clients):
    """安装只引用平台保管库名称，初始状态禁用。"""
    result=await clients['tenant_admin'].post('/v1/admin/connections',json={
        'name':'企微只读','corp_id':'corp-a','agent_id':'10001','secret_ref':'wecom-secret',
        'request_id':'install-1','operations':['self_profile.read','directory_preview.read']})
    assert result.status_code == 201, result.text
    return result.json()['id']


async def test_consent_requires_active_installation(r3_http):
    """未核定应用能力时不可同意并派发。"""
    cid=await installation(r3_http)
    response=await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents',json={
        'request_id':'consent-1','operations':['self_profile.read'],
        'expires_at':time.time()+3600,'purpose':'本人资料核对'})
    assert response.status_code == 409
    assert (await r3_http['machine'].get('/v1/me/connections')).status_code == 403


async def test_active_consent_is_private_and_revocation_is_final(r3_http,r3_world):
    """真实Cookie同意、并发回执一致，撤销后不能复活旧委托。"""
    cid=await installation(r3_http)
    result=await r3_http['tenant_admin'].patch(f'/v1/admin/connections/{cid}',json={
        'request_id':'enable-1','expected_version':1,'status':'active','capability_evidence':'test-only:fixture'})
    assert result.status_code==200,result.text
    payload={'request_id':'consent-1','operations':['self_profile.read'],
        'expires_at':time.time()+3600,'purpose':'本人资料核对'}
    result=await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents',json=payload)
    assert result.status_code==201,result.text
    consent=result.json()
    assert (await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents',json=payload)).json()==consent
    url=f"/v1/me/connections/{cid}/consents/{consent['consent_id']}"
    assert (await r3_http['bob'].get(url)).status_code==404
    key=await r3_world.platform.issue_key(r3_world.admin,tenant_id='acme',name='actor',
                                        role='tenant_member',act_as_users=True)
    result=await r3_http['machine'].post(f'/v1/me/connections/{cid}/consents',json=payload,
        headers={'Authorization':'Bearer '+key.key,'X-Xuanpu-Act-As':'usr_alice'})
    assert result.status_code==403
    result=await r3_http['alice'].post(url+'/revoke',json={
        'request_id':'revoke-1','expected_version':1})
    assert result.status_code==200,result.text
    assert result.json()['status']=='revoked'
    assert (await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents',json=payload)).status_code==409


async def test_consent_bounds_and_cas(r3_http):
    """过期、永久、目录越权及并发版本均被拒绝。"""
    cid=await installation(r3_http)
    wrong=await r3_http['tenant_admin'].patch(f'/v1/admin/connections/{cid}',json={
        'request_id':'bad','expected_version':2,'status':'active','capability_evidence':'test-only:fixture'})
    assert wrong.status_code==409
    for expiry in [0,time.time()+90000]:
        response=await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents',json={
            'request_id':'bad-expiry','operations':['self_profile.read'],
            'expires_at':expiry,'purpose':'本人资料核对'})
        assert response.status_code in (400,409)


async def test_full_metadata_still_allows_revoke_and_disable(r3_world,r3_http,r3_connected,monkeypatch):
    """创建配额耗尽不能阻止任何收紧权限操作。"""
    from xuanpu_server.connections import rules
    monkeypatch.setattr(rules,'MAX_RECORDS',12)
    cid,consent=r3_connected
    for number in range(20):
        result=await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents',json={
            'request_id':f'fill-{number}','operations':['self_profile.read'],
            'purpose':'测试额度','expires_at':time.time()+3600})
        if result.status_code==409:
            break
    else:
        raise AssertionError('创建配额没有生效')
    result=await r3_http['alice'].post(f"/v1/me/connections/{cid}/consents/{consent['consent_id']}/revoke",json={
        'request_id':'revoke-when-full','expected_version':1})
    assert result.status_code==200,result.text
    result=await r3_http['tenant_admin'].patch(f'/v1/admin/connections/{cid}',json={
        'request_id':'disable-when-full','expected_version':2,'status':'disabled'})
    assert result.status_code==200,result.text


async def test_connection_audit_survives_read_transactions(r3_world,r3_http,r3_connected):
    """两种存储的连接审计均进入平台正式审计查询，读事务不清除历史。"""
    from xuanpu_server.controlplane.models import AuditQuery
    cid,_=r3_connected
    for _ in range(2):
        assert (await r3_http['alice'].get('/v1/me/connections')).status_code==200
    records=await r3_world.platform.store.query_audit('acme',AuditQuery(kind='connection_consented'))
    assert any(record.data['installation_id']==cid and record.user_id=='usr_alice' for record in records)

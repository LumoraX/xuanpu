"""明确映射已有员工，不能按同名员工自动合并。"""
from __future__ import annotations


async def test_wecom_account_binding_is_admin_only_and_unique(r3_http):
    """管理员指定稳定usr；同一外部主体不能抢占其他员工。"""
    response=await r3_http['tenant_admin'].post('/v1/admin/connections',json={
        'name':'企微','corp_id':'corp-a','agent_id':'1','secret_ref':'wecom-secret',
        'request_id':'bind-install','operations':['self_profile.read']})
    cid=response.json()['id']
    url=f'/v1/admin/connections/{cid}/accounts'
    assert (await r3_http['alice'].post(url,json={'user_id':'usr_alice','userid':'alice'})).status_code==403
    result=await r3_http['tenant_admin'].post(url,json={'user_id':'usr_alice','userid':'alice'})
    assert result.status_code==201,result.text
    assert result.json()['user_id']=='usr_alice'
    result=await r3_http['tenant_admin'].post(url,json={'user_id':'usr_bob','userid':'ALICE'})
    assert result.status_code==409

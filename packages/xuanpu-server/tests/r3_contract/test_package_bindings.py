"""不可变包绑定限制固定只读目的端与认证参数。"""
from __future__ import annotations


async def test_package_binding_is_immutable_and_read_only(r3_http,r3_connected,r3_package):
    """同一包operation不能偷偷换另一个员工操作或endpoint。"""
    cid,_=r3_connected
    url=f'/v1/admin/connections/{cid}/package-bindings'
    body={'request_id':'bind-1','package_digest':r3_package.digest.removeprefix('sha256:'),'connector_name':'wecom',
          'operation_id':'self_profile','operation':'self_profile.read',
          'method':'GET','endpoint':'/cgi-bin/user/get','effect':'read',
          'server_bound_parameters':['userid','access_token']}
    response=await r3_http['tenant_admin'].post(url,json=body)
    assert response.status_code==201,response.text
    response=await r3_http['tenant_admin'].post(url,json={**body,'request_id':'bind-2','endpoint':'/cgi-bin/department/list'})
    assert response.status_code in (400,409)
    response=await r3_http['alice'].post(url,json=body)
    assert response.status_code==403
    response=await r3_http['tenant_admin'].post(url,json={**body,'method':'POST'})
    assert response.status_code==400


async def test_unpublished_digest_is_rejected(r3_http,r3_connected):
    """仅管理员声明摘要不足以核定业务包，必须已发布。"""
    cid,_=r3_connected
    response=await r3_http['tenant_admin'].post(f'/v1/admin/connections/{cid}/package-bindings',json={
        'request_id':'unknown-package','package_digest':'f'*64,'connector_name':'wecom',
        'operation_id':'self_profile','operation':'self_profile.read','endpoint':'/cgi-bin/user/get',
        'server_bound_parameters':['userid','access_token']})
    assert response.status_code==404

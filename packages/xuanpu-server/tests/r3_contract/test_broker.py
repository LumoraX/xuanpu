"""逐调用代理对撤销、员工和token更新的拒绝性测试。"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from xuanpu_server.errors import PermissionDeniedError
from xuanpu_server.tenancy import Principal


async def test_missing_consent_has_no_external_request(r3_world):
    """不满足平台委托时连取token都不得发生。"""
    assert hasattr(r3_world.platform,'connection_broker')
    broker=r3_world.platform.connection_broker
    with pytest.raises(PermissionDeniedError):
        await broker.read(Principal(role='tenant_member',tenant_id='acme',user_id='usr_missing',
                                   key_id=None),'unknown','self_profile.read',{})


async def test_invalid_subject_override_never_reaches_provider(r3_http):
    """保护参数在API层和工具层都拒绝。"""
    response=await r3_http['alice'].post('/v1/me/connections/unknown/read',json={
         'operation':'self_profile.read','parameters':{'userid':'other'}})
    assert response.status_code==400


async def test_inflight_revocation_discards_result(r3_http,r3_world,r3_connected):
    """请求已到供应商时尽力取消；撤销后迟到正文不能交给浏览器或模型。"""
    from xuanpu_server.connections.providers.wecom import WeComClient
    platform=r3_world.platform
    started,release=asyncio.Event(),asyncio.Event()
    calls=[]
    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('gettoken'):
            return httpx.Response(200,json={'errcode':0,'access_token':'token','expires_in':7200})
        started.set()
        await release.wait()
        return httpx.Response(200,json={'errcode':0,'userid':'alice','name':'late-private-name'})
    provider=WeComClient(transport=httpx.MockTransport(handler))
    platform.connection_tokens.provider=provider
    platform.connection_broker.provider=provider
    cid,consent=r3_connected
    try:
        pending=asyncio.create_task(r3_http['alice'].post(f'/v1/me/connections/{cid}/read',json={
            'operation':'self_profile.read','parameters':{}}))
        await asyncio.wait_for(started.wait(),5)
        response=await r3_http['alice'].post(f"/v1/me/connections/{cid}/consents/{consent['consent_id']}/revoke",json={
            'request_id':'revoke-inflight','expected_version':1})
        assert response.status_code==200,response.text
        release.set()
        response=await pending
        assert response.status_code==403,response.text
        assert 'late-private-name' not in response.text
        assert (await r3_http['alice'].post(f'/v1/me/connections/{cid}/read',json={
            'operation':'self_profile.read'})).status_code==403
        assert len(calls)==2
    finally:
        release.set()
        await provider.close()


async def test_self_read_isolation_and_role_recheck(r3_http,r3_world,r3_connected):
    """同runtime也不能用Alice委托替Bob读取，普通员工不能读通讯录。"""
    from xuanpu_server.connections.providers.wecom import WeComClient
    calls=[]
    async def handler(request):
        calls.append(request)
        data=({'errcode':0,'access_token':'token','expires_in':7200} if request.url.path.endswith('gettoken')
              else {'errcode':0,'userid':request.url.params['userid'],'name':'Alice','mobile':'secret'})
        return httpx.Response(200,json=data)
    provider=WeComClient(transport=httpx.MockTransport(handler))
    platform=r3_world.platform
    platform.connection_tokens.provider=provider
    platform.connection_broker.provider=provider
    cid,_=r3_connected
    try:
        assert (await r3_http['bob'].post(f'/v1/me/connections/{cid}/read',json={
            'operation':'self_profile.read'})).status_code==403
        assert (await r3_http['alice'].post(f'/v1/me/connections/{cid}/read',json={
            'operation':'directory_preview.read'})).status_code==403
        assert not calls
        result=await r3_http['alice'].post(f'/v1/me/connections/{cid}/read',json={'operation':'self_profile.read'})
        assert result.status_code==200,result.text
        assert result.json()['data']['userid']=='alice'
        assert 'secret' not in result.text
    finally:
        await provider.close()


async def test_directory_stops_between_provider_requests_on_revoke(r3_http,r3_world,r3_connected):
    """目录首个响应在途撤销后，不能再派发任何成员分页请求。"""
    from xuanpu_server.connections.providers.wecom import WeComClient
    cid,_=r3_connected
    response=await r3_http['tenant_admin'].post(f'/v1/me/connections/{cid}/consents',json={
        'request_id':'directory','operations':['directory_preview.read'],
        'expires_at':time.time()+3600,'purpose':'管理员只读预览'})
    consent=response.json()
    started,release=asyncio.Event(),asyncio.Event()
    calls=[]
    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('gettoken'):
            return httpx.Response(200,json={'errcode':0,'access_token':'token','expires_in':7200})
        if request.url.path.endswith('/department/list'):
            started.set();await release.wait()
            return httpx.Response(200,json={'errcode':0,'department':[{'id':1,'name':'部门'}]})
        return httpx.Response(200,json={'errcode':0,'userlist':[]})
    provider=WeComClient(transport=httpx.MockTransport(handler))
    platform=r3_world.platform
    platform.connection_tokens.provider=provider;platform.connection_broker.provider=provider
    try:
        pending=asyncio.create_task(r3_http['tenant_admin'].post(f'/v1/me/connections/{cid}/read',json={
            'operation':'directory_preview.read'}))
        await asyncio.wait_for(started.wait(),5)
        response=await r3_http['tenant_admin'].post(f"/v1/me/connections/{cid}/consents/{consent['consent_id']}/revoke",json={
            'request_id':'revoke-directory','expected_version':1})
        assert response.status_code==200,response.text
        release.set()
        result=await pending
        assert result.status_code==403
        assert '/cgi-bin/user/simplelist' not in calls
    finally:
        release.set();await provider.close()


@pytest.mark.parametrize('change',['role','key'])
async def test_directory_rechecks_current_role_and_key_between_pages(r3_http,r3_world,r3_connected,change):
    """权限在目录首响应期间收窄，后续页面与迟到正文均不能继续。"""
    from xuanpu_server.connections.providers.wecom import WeComClient
    from xuanpu_server.tenancy import ApiKeyRecord
    platform=r3_world.platform
    cid,_=r3_connected
    response=await r3_http['tenant_admin'].post(f'/v1/me/connections/{cid}/consents',json={
        'request_id':'directory-role','operations':['directory_preview.read'],
        'expires_at':time.time()+3600,'purpose':'管理员预览'})
    assert response.status_code==201,response.text
    key='abcdef012345'
    await platform.store.create_api_key(ApiKeyRecord(key,'acme','act-as','tenant_admin','hash',time.time(),can_act_as_user=True))
    who=Principal(role='tenant_admin',tenant_id='acme',user_id='usr_tenant_admin',
                  key_id=key if change=='key' else None)
    started,release=asyncio.Event(),asyncio.Event()
    calls=[]
    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('gettoken'):
            return httpx.Response(200,json={'errcode':0,'access_token':'token','expires_in':7200})
        if request.url.path.endswith('/department/list'):
            started.set();await release.wait()
            return httpx.Response(200,json={'errcode':0,'department':[{'id':1,'name':'private-department'}]})
        return httpx.Response(200,json={'errcode':0,'userlist':[{'userid':'other','name':'private-employee'}]})
    provider=WeComClient(transport=httpx.MockTransport(handler))
    platform.connection_tokens.provider=provider;platform.connection_broker.provider=provider
    try:
        pending=asyncio.create_task(platform.connection_broker.read(who,cid,'directory_preview.read',{}))
        await asyncio.wait_for(started.wait(),5)
        if change=='role':
            await platform.identity.store.set_user_role('acme','usr_tenant_admin','tenant_member')
        else:
            await platform.store.revoke_api_key('acme',key,at=time.time())
        release.set()
        with pytest.raises(PermissionDeniedError):
            await pending
        assert '/cgi-bin/user/simplelist' not in calls
    finally:
        release.set();await provider.close()

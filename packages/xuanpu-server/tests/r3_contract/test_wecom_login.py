"""企微回调的同浏览器、一次消费和稳定usr。"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx


async def test_wrong_browser_state_is_consumed(r3_http,r3_world):
    """跨浏览器state不能绑定，且失败后不能重放。"""
    response=await r3_http['tenant_admin'].post('/v1/admin/connections',json={
        'name':'企微','corp_id':'corp-a','agent_id':'1','secret_ref':'wecom-secret',
        'request_id':'login-install','operations':['self_profile.read']})
    cid=response.json()['id']
    await r3_http['tenant_admin'].patch(f'/v1/admin/connections/{cid}',json={
        'request_id':'enable','expected_version':1,'status':'active','capability_evidence':'test-only'})
    url=f'/v1/auth/wecom/start?tenant=acme&connection={cid}'
    response=await r3_http['alice'].get(url)
    assert response.status_code==302,response.text
    state=parse_qs(urlsplit(response.headers['location']).query)['state'][0]
    callback='/v1/auth/wecom/callback?state='+state+'&code=code-a'
    assert (await r3_http['bob'].get(callback)).status_code==401
    assert (await r3_http['alice'].get(callback)).status_code==401


async def test_login_uses_real_identity_exchange_and_keeps_usr(r3_http,r3_world):
    """合成供应商只证明协议；不把Cookie模拟称为真实扫码。"""
    from xuanpu_server.connections.providers.wecom import WeComClient
    platform=r3_world.platform
    calls=[]
    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('gettoken'):
            data={'errcode':0,'access_token':'token','expires_in':7200}
        elif request.url.path.endswith('getuserinfo'):
            data={'errcode':0,'userid':'alice'}
        else:
            data={'errcode':0,'userid':'alice','name':'供应商姓名'}
        return httpx.Response(200,json=data)
    provider=WeComClient(transport=httpx.MockTransport(handler))
    platform.connection_tokens.provider=provider
    platform.wecom_login.provider=provider
    await platform.vault.put('acme','wecom-secret','secret',actor='test')
    response=await r3_http['tenant_admin'].post('/v1/admin/connections',json={
        'name':'企微','corp_id':'corp-a','agent_id':'1','secret_ref':'wecom-secret',
        'request_id':'login-install','operations':['self_profile.read']})
    cid=response.json()['id']
    await r3_http['tenant_admin'].patch(f'/v1/admin/connections/{cid}',json={
        'request_id':'enable','expected_version':1,'status':'active','capability_evidence':'test-only'})
    await r3_http['tenant_admin'].post(f'/v1/admin/connections/{cid}/accounts',json={
        'user_id':'usr_alice','userid':'alice'})
    try:
        response=await r3_http['alice'].get(f'/v1/auth/wecom/start?tenant=acme&connection={cid}')
        state=parse_qs(urlsplit(response.headers['location']).query)['state'][0]
        response=await r3_http['alice'].get('/v1/auth/wecom/callback?state='+state+'&code=code-a')
        assert response.status_code==302,response.text
        assert response.headers['location']=='/console/'
        me=await r3_http['alice'].get('/v1/me')
        assert me.json()['user_id']=='usr_alice'
        assert me.json()['display_name']=='同名员工'
        assert calls==['/cgi-bin/gettoken','/cgi-bin/auth/getuserinfo','/cgi-bin/user/get']
    finally:
        await provider.close()


async def test_logout_during_binding_prevents_identity_and_new_session(r3_http,r3_world,r3_connected):
    """供应商响应在途期间登出，完成事务必须拒绝旧会话。"""
    import asyncio

    from xuanpu_server.connections.account_bindings import subject
    from xuanpu_server.connections.providers.wecom import WeComClient
    from xuanpu_server.identity.cookies import login_token_hash
    cid,_=r3_connected
    platform=r3_world.platform
    started,release=asyncio.Event(),asyncio.Event()
    async def handler(request):
        if request.url.path.endswith('gettoken'):
            return httpx.Response(200,json={'errcode':0,'access_token':'token','expires_in':7200})
        if request.url.path.endswith('getuserinfo'):
            started.set();await release.wait()
            return httpx.Response(200,json={'errcode':0,'userid':'bob-new'})
        return httpx.Response(200,json={'errcode':0,'userid':'bob-new'})
    provider=WeComClient(transport=httpx.MockTransport(handler))
    platform.connection_tokens.provider=provider;platform.wecom_login.provider=provider
    try:
        result=await r3_http['bob'].post(f'/v1/me/connections/{cid}/binding/start',json={})
        state=parse_qs(urlsplit(result.json()['authorization_url']).query)['state'][0]
        pending=asyncio.create_task(r3_http['bob'].get('/v1/auth/wecom/callback?state='+state+'&code=once'))
        await asyncio.wait_for(started.wait(),5)
        await platform.identity.store.delete_login_session('acme',login_token_hash('test-session-bob'))
        release.set()
        response=await pending
        assert response.status_code==401,response.text
        assert await platform.identity.store.get_user_by_identity('acme','wecom',subject('corp-a','bob-new')) is None
    finally:
        release.set();await provider.close()


async def test_installation_revision_during_oauth_rejects_old_attempt(r3_http,r3_world,r3_connected):
    """供应商在途停用再启用，不能用旧attempt完成绑定或登录。"""
    import asyncio

    from xuanpu_server.connections.account_bindings import subject
    from xuanpu_server.connections.providers.wecom import WeComClient
    cid,_=r3_connected
    platform=r3_world.platform
    started,release=asyncio.Event(),asyncio.Event()
    async def handler(request):
        if request.url.path.endswith('gettoken'):
            return httpx.Response(200,json={'errcode':0,'access_token':'token','expires_in':7200})
        if request.url.path.endswith('getuserinfo'):
            started.set();await release.wait()
        return httpx.Response(200,json={'errcode':0,'userid':'bob-new'})
    provider=WeComClient(transport=httpx.MockTransport(handler))
    platform.connection_tokens.provider=provider;platform.wecom_login.provider=provider
    try:
        result=await r3_http['bob'].post(f'/v1/me/connections/{cid}/binding/start',json={})
        state=parse_qs(urlsplit(result.json()['authorization_url']).query)['state'][0]
        pending=asyncio.create_task(r3_http['bob'].get('/v1/auth/wecom/callback?state='+state+'&code=once'))
        await asyncio.wait_for(started.wait(),5)
        for version,status in [(2,'disabled'),(3,'active')]:
            response=await r3_http['tenant_admin'].patch(f'/v1/admin/connections/{cid}',json={
                'request_id':'oauth-'+status,'expected_version':version,'status':status,
                'capability_evidence':'synthetic only'})
            assert response.status_code==200,response.text
        release.set()
        response=await pending
        assert response.status_code==409,response.text
        assert 'set-cookie' not in response.headers
        assert await platform.identity.store.get_user_by_identity('acme','wecom',subject('corp-a','bob-new')) is None
    finally:
        release.set();await provider.close()

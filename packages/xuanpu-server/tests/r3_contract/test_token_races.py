"""令牌持久缓存及双后端CAS并发验证。"""
from __future__ import annotations

import asyncio

import httpx


async def test_token_refresh_singleflight_and_ciphertext(r3_http,r3_world):
    """并发读取相同应用仅更新一次，数据库看不到明文token。"""
    from xuanpu_server.connections.providers.wecom import WeComClient
    from xuanpu_server.connections.tokens import TokenBroker
    calls=[]
    async def handler(request):
        calls.append(request)
        await asyncio.sleep(.02)
        return httpx.Response(200,json={'errcode':0,'access_token':'hidden-app-token','expires_in':7200})
    platform=r3_world.platform
    await platform.vault.put('acme','wecom-secret','hidden-secret',actor='test')
    response=await r3_http['tenant_admin'].post('/v1/admin/connections',json={
       'request_id':'token-install','name':'企微','corp_id':'corp-a','agent_id':'10001',
       'secret_ref':'wecom-secret','operations':['self_profile.read']})
    cid=response.json()['id']
    await r3_http['tenant_admin'].patch(f'/v1/admin/connections/{cid}',json={
       'request_id':'enable','expected_version':1,'status':'active','capability_evidence':'test-only'})
    provider=WeComClient(transport=httpx.MockTransport(handler))
    broker=TokenBroker(platform.connections,platform.vault,provider)
    try:
        tokens=await asyncio.gather(*(broker.get('acme',cid) for _ in range(8)))
        assert tokens==['hidden-app-token']*8
        assert len(calls)==1
        # 冷重建也使用持久缓存，不能只依赖进程内锁。
        other=TokenBroker(platform.connections,platform.vault,provider)
        assert await other.get('acme',cid)=='hidden-app-token'
        async with platform.store.connection_store.transaction('acme') as state:
            assert 'hidden-app-token' not in str(state.records)
            assert all(b'hidden-app-token' not in c for c in state.credentials.values())
        assert 'hidden-app-token' not in str(await platform.vault.names('acme'))
    finally:
        await provider.close()

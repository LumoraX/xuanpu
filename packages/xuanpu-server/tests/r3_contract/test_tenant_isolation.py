"""双企业同名、相同usr仍不能借用另一企业的安装与委托。"""
from __future__ import annotations

import httpx
from xuanpu_server.connections.providers.wecom import WeComClient


async def test_shared_usr_and_name_do_not_cross_enterprise(r3_world,r3_http,r3_connected,r3_beta):
    """经真实Cookie/API读取，拒绝发生在任何供应商请求之前。"""
    cid,consent=r3_connected
    response=await r3_beta['tenant_admin'].post('/v1/admin/connections',json={
        'request_id':'beta-install','name':'另一企业企微','corp_id':'corp-b','agent_id':'2',
        'secret_ref':'beta-secret','operations':['self_profile.read']})
    assert response.status_code==201,response.text
    beta_id=response.json()['id']
    calls=[]
    async def handler(request):
        """跨企业请求不得到达此供应商端点。"""
        calls.append(request.url.path)
        return httpx.Response(200,json={'errcode':0})
    provider=WeComClient(transport=httpx.MockTransport(handler))
    platform=r3_world.platform
    platform.connection_tokens.provider=provider;platform.connection_broker.provider=provider
    try:
        response=await r3_beta['alice'].get('/v1/me/connections')
        assert [row['id'] for row in response.json()['connections']]==[beta_id]
        assert (await r3_beta['alice'].get(f"/v1/me/connections/{cid}/consents/{consent['consent_id']}")).status_code==404
        assert (await r3_beta['alice'].post(f'/v1/me/connections/{cid}/read',json={'operation':'self_profile.read'})).status_code==404
        assert (await r3_http['alice'].post(f'/v1/me/connections/{beta_id}/read',json={'operation':'self_profile.read'})).status_code==404
        assert calls==[]
    finally:
        await provider.close()

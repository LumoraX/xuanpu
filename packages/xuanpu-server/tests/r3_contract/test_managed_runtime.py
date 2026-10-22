"""真实内核工具调用和本机HTTP供应商；不等同真实企业微信。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from xuanpu_server.connections.providers.wecom import WeComClient
from xuanpu_server.tenancy import Principal
from xuanpu_spec import pack_package


class LocalWire(httpx.AsyncBaseTransport):
    """仅测试把固定供应商请求转送到真正的本机HTTP服务。"""
    def __init__(self,port):
        self.port=port
        self.http=httpx.AsyncClient(trust_env=False)

    async def handle_async_request(self,request):
        url=request.url.copy_with(scheme='http',host='127.0.0.1',port=self.port)
        return await self.http.get(url)

    async def aclose(self):
        await self.http.aclose()


async def test_managed_tool_uses_current_actor_and_never_exposes_token(r3_world,r3_http,r3_connected,step,read_turn):
    """没有连接器静态密钥，内核仍经当前员工broker读取；模型拿不到token。"""
    calls=[]
    async def remote(reader,writer):
        header=await reader.readuntil(b'\r\n\r\n')
        method,target,_=header.split(b'\r\n',1)[0].decode().split(' ')
        route=urlsplit(target)
        query=parse_qs(route.query)
        calls.append((method,route.path,query))
        data=({'errcode':0,'access_token':'app-private-token','expires_in':7200}
              if route.path.endswith('gettoken') else
              {'errcode':0,'userid':query['userid'][0],'name':'核对员工','mobile':'not-permitted'})
        body=json.dumps(data).encode()
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
        await writer.drain();writer.close();await writer.wait_closed()
    server=await asyncio.start_server(remote,'127.0.0.1',0)
    provider=WeComClient(transport=LocalWire(server.sockets[0].getsockname()[1]))
    platform=r3_world.platform
    platform.connection_tokens.provider=provider;platform.connection_broker.provider=provider
    source=Path(__file__).resolve().parents[4]/'examples/wecom-readonly'
    package=await platform.packages.publish(r3_world.admin,pack_package(source),activate=True)
    cid,_=r3_connected
    result=await r3_http['tenant_admin'].post(f'/v1/admin/connections/{cid}/package-bindings',json={
        'request_id':'real-runtime','package_digest':package.digest.removeprefix('sha256:'),
        'connector_name':'wecom','operation_id':'self_profile','operation':'self_profile.read',
        'endpoint':'/cgi-bin/user/get','server_bound_parameters':['userid','access_token']})
    assert result.status_code==201,result.text
    who=Principal(role='tenant_member',tenant_id='acme',key_id=None,user_id='usr_alice')
    try:
        session=await platform.sessions.create(who,package='wecom-readonly',entry='read')
        r3_world.models.script(step(calls=[('c1','api__wecom__self_profile',{})]),step(text='已核对'))
        events=await read_turn(await platform.sessions.send_message(who,session.session_id,'核对本人资料'))
        output=next(e.msg.data for e in events if e.msg.kind=='tool_call_completed')
        assert output['is_error'] is False,output
        assert '核对员工' in output['output']
        prompts=json.dumps([r.model_dump() for r in r3_world.models.requests],ensure_ascii=False,default=str)
        assert 'app-private-token' not in prompts
        assert 'not-permitted' not in prompts
        assert all(method=='GET' for method,_,_ in calls)
        assert calls[-1][2]['userid']==['alice']
    finally:
        server.close();await server.wait_closed();await provider.close()

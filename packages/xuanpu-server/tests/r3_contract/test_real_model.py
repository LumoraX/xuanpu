"""显式真实模型验收；企微仍为合成协议端点，不冒充真实接入。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values
from xuanpu_server.connections.providers.wecom import WeComClient
from xuanpu_server.runtime.models import GatewayConfig, GatewayModelClientFactory, ModelProfile
from xuanpu_server.tenancy import Principal

MODEL_ENV=os.environ.get('XUANPU_R3_REAL_MODEL_ENV')
pytestmark=[pytest.mark.skipif(not MODEL_ENV,reason='真实模型需显式指定私有配置'),
            pytest.mark.parametrize('identity_stores',['postgres'],indirect=True)]


class RecordedFactory:
    """只保存公开模型协议；不接触认证头。"""
    def __init__(self,inner):
        """注入原客户端工厂并建立本轮记录。"""
        self.inner,self.calls=inner,[]

    async def create(self,tenant,tier):
        """包装公开客户端协议，不保存认证信息。"""
        inner=await self.inner.create(tenant,tier)
        records=self.calls
        class Client:
            """只观察实际采样请求。"""
            capabilities=getattr(inner,'capabilities',None)
            def session(self,**kwargs):
                """为每个真实会话建立观察器。"""
                session=inner.session(**kwargs)
                class Session:
                    """保留原模型会话生命周期。"""
                    async def __aenter__(self):
                        """进入真实客户端会话。"""
                        await session.__aenter__();return self
                    async def __aexit__(self,*args):
                        """退出并关闭底层会话资源。"""
                        await session.__aexit__(*args)
                    async def stream(self,request):
                        """保存原始公开协议事件，再交给内核。"""
                        record={'request':request.model_dump(mode='json'),'events':[]}
                        records.append(record)
                        async for event in session.stream(request):
                            record['events'].append({'kind':event.kind,'data':event.data})
                            yield event
                return Session()
        return Client()


async def test_real_model_reads_managed_profile_without_credentials(r3_world,r3_http,r3_connected,r3_package,read_turn):
    """真实网关发起实际内核调用，检查权限投影与凭据不进入模型上下文。"""
    env=dotenv_values(MODEL_ENV)
    assert env.get('LLM_BOOTSTRAP_PROTOCOL') in {'chat','responses','codex_responses'}
    platform=r3_world.platform
    factory=RecordedFactory(GatewayModelClientFactory(GatewayConfig(
        base_url=env['LLM_BOOTSTRAP_BASE_URL'],api_key=env['LLM_BOOTSTRAP_API_KEY'],
        protocol=env['LLM_BOOTSTRAP_PROTOCOL'],profiles={tier:ModelProfile(env['LLM_BOOTSTRAP_MODEL'],timeout_seconds=90)
        for tier in ('fast','balanced','deep')}),platform.vault))
    platform.runtimes._models=factory
    calls=[]
    async def handler(request):
        """合成企微只读端点，敏感字段用于检验投影。"""
        calls.append(request.url.path)
        data=({'access_token':'synthetic-app-private-token','expires_in':7200}
            if request.url.path.endswith('gettoken') else
            {'userid':'alice','name':'合成验收员工','position':'资料核对','department':[1],
             'mobile':'synthetic-private-phone','user_ticket':'synthetic-private-ticket'})
        return httpx.Response(200,json={'errcode':0,**data})
    provider=WeComClient(transport=httpx.MockTransport(handler))
    platform.connection_tokens.provider=provider;platform.connection_broker.provider=provider
    cid,_=r3_connected
    response=await r3_http['tenant_admin'].post(f'/v1/admin/connections/{cid}/package-bindings',json={
        'request_id':'real-model','package_digest':r3_package.digest.removeprefix('sha256:'),
        'connector_name':'wecom','operation_id':'self_profile','operation':'self_profile.read',
        'endpoint':'/cgi-bin/user/get','server_bound_parameters':['userid','access_token']})
    assert response.status_code==201,response.text
    who=Principal(role='tenant_member',tenant_id='acme',key_id=None,user_id='usr_alice')
    output=Path(os.environ['XUANPU_R3_EVIDENCE_DIR'])
    output.mkdir(parents=True,exist_ok=False)
    assessment={'real_model':'not_checked','real_wecom':'not_checked','provider':'synthetic','external_write_calls':0}
    try:
        session=await platform.sessions.create(who,package='wecom-readonly',entry='read')
        events=await read_turn(await platform.sessions.send_message(who,session.session_id,
            '请实际调用本人资料只读工具，核对姓名、岗位、部门及缺失字段；不要只说明操作方法。'))
        tools=[e.msg.data for e in events if e.msg.kind=='tool_call_completed']
        assert any(not tool.get('is_error') and '合成验收员工' in tool.get('output','') for tool in tools),tools
        raw=json.dumps(factory.calls,ensure_ascii=False,default=str)
        assert all(secret not in raw for secret in ('synthetic-app-private-token','synthetic-private-phone','synthetic-private-ticket'))
        assert calls.count('/cgi-bin/user/get')>=1
        assessment.update(real_model='passed',model_calls=len(factory.calls),provider_read_calls=calls.count('/cgi-bin/user/get'))
    finally:
        (output/'raw-model.json').write_text(json.dumps(factory.calls,ensure_ascii=False,indent=2,default=str))
        (output/'assessment.json').write_text(json.dumps(assessment,ensure_ascii=False,indent=2))
        await provider.close()

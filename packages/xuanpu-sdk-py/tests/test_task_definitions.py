"""客户端安排接口传递原请求，不扩张身份或自动重新授权。"""
from __future__ import annotations

import httpx
from xuanpu_sdk import XuanpuClient


async def test_definition_management_and_effect_query_preserve_original_request():
    """真实SDK经HTTP边界传递固定request_id、CAS及代员工身份。"""
    calls=[]
    def receive(request):
        calls.append(request)
        if request.url.path.endswith('/occurrences'):return httpx.Response(200,json={'items':[]})
        if request.url.path.endswith('/reconcile'):return httpx.Response(200,json={'state':'confirmed_success'})
        if request.method=='GET' and request.url.path=='/v1/task-definitions':return httpx.Response(200,json={'items':[]})
        return httpx.Response(200,json={'definition_id':'def','version':1,'state_version':1,'enabled':True,'title':'本人安排'})
    async with XuanpuClient('https://example.test','PRIVATE_KEY',act_as_user='member',transport=httpx.MockTransport(receive)) as client:
        assert hasattr(client,'create_task_definition'),'SDK尚无执行安排入口'
        body={'request_id':'stable','confirmed':True,'title':'本人安排'}
        await client.create_task_definition(body)
        await client.create_task_definition(body)
        await client.get_task_definition('a/b')
        await client.list_task_definitions()
        await client.update_task_definition('def',{**body,'expected_version':1})
        await client.pause_task_definition('def',request_id='p',expected_version=1)
        await client.resume_task_definition('def',request_id='r',expected_version=2)
        await client.cancel_task_definition('def',request_id='c',expected_version=3)
        assert await client.list_occurrences('def')==[]
        await client.reconcile_task_effect('task','action',request_id='verify',expected_version=4)
    assert calls[0].content==calls[1].content
    assert calls[2].url.raw_path==b'/v1/task-definitions/a%2Fb'
    assert all(r.headers['X-Xuanpu-Act-As']=='member' for r in calls)

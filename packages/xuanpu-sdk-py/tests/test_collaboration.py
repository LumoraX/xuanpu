"""协作客户端不自动确认、改版本、换员工或把组织决定当作工具批准。"""
from __future__ import annotations

import json

import httpx
import pytest
from xuanpu_sdk import XuanpuClient


async def test_collaboration_requests_preserve_identity_confirmation_and_fixed_result():
    """记录实际HTTP边界，客户端不能替调用者补授权。"""
    calls=[]
    def receive(request):
        """仅作为传输契约测试，不冒充真实业务验收。"""
        calls.append(request);return httpx.Response(200,json={'id':'receipt'})
    body={'request_id':'fixed','confirmed':False,'expected_version':3,'delivery_version':1}
    async with XuanpuClient('https://example.test','synthetic',act_as_user='employee',transport=httpx.MockTransport(receive)) as client:
        await client.handoff_action('a/b','sign-off',body)
        await client.handoff_action('a/b','sign-off',body)
        await client.approval_decision('approval','approve',{'request_id':'org','confirmed':True,'expected_version':1})
        await client.upload_handoff_result('handoff',b'fixed',name='成果.txt',mime_type='text/plain',request_id='result',
            expected_version=4,shared_version=123,confirmed=False)
        with pytest.raises(ValueError):
            await client.handoff_action('handoff','../approve',body)
    assert calls[0].content==calls[1].content and json.loads(calls[0].content)['confirmed'] is False
    assert calls[0].url.raw_path==b'/v1/handoffs/a%2Fb/sign-off'
    assert calls[2].url.path=='/v1/approval-requests/approval/approve'
    assert calls[3].content==b'fixed' and calls[3].url.params['confirmed']=='false'
    assert calls[3].url.params['shared_version']=='123'
    assert all(r.headers['X-Xuanpu-Act-As']=='employee' for r in calls)

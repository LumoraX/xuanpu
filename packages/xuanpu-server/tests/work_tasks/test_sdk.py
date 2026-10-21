"""SDK保留服务端受理与未知状态，不推断成功或自动重试写入。"""

from __future__ import annotations

import json

import httpx
import pytest
from xuanpu_sdk import XuanpuClient, XuanpuError


async def test_work_task_contracts_and_unknown_status():
    """六个调用对应真实接口形状，受理和取消均不能被改写为成功。"""
    seen = []
    task = {'task_id':'t1','status':'outcome_unknown','state_version':3,'text':'汇总',
            'artifacts':[],'pending':[]}

    def handle(request):
        """只返回服务端状态，记录客户端实际传输。"""
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path.endswith('/events'):
            return httpx.Response(200,json={'items':[{'event_id':2,'status':'running'}]})
        if request.url.path == '/v1/work-tasks' and request.method == 'GET':
            return httpx.Response(200,json={'items':[task]})
        return httpx.Response(202 if request.method == 'POST' else 200,json=task)

    async with XuanpuClient('http://test','key',transport=httpx.MockTransport(handle)) as client:
        created = await client.create_work_task('汇总',[{'item_id':'f1','revision':2}],request_id='stable')
        assert created.status == 'outcome_unknown'
        assert (await client.get_work_task('t1')).task_id == 't1'
        assert len(await client.list_work_tasks()) == 1
        assert (await client.work_task_events('t1',after_id=1))[0]['event_id'] == 2
        await client.cancel_work_task('t1')
        await client.decide_work_task('t1',{'kind':'office_actions','request_id':'d','state_version':3,'choices':[]})
    assert len(seen) == 6
    assert seen[0][2]['request_id'] == 'stable' and seen[0][2]['input_files'][0]['revision'] == 2


@pytest.mark.parametrize('status', [403,404,409])
async def test_work_task_errors_are_not_retried(status):
    """无权限、缺失和版本冲突都保留原HTTP错误。"""
    calls = []

    def handle(request):
        """写失败一次返回一次，不在SDK隐式重发。"""
        calls.append(request)
        return httpx.Response(status,json={'error':{'code':'conflict','message':'请刷新'}})

    async with XuanpuClient('http://test','key',transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(XuanpuError) as error:
            await client.cancel_work_task('t1')
        assert error.value.status == status
    assert len(calls) == 1

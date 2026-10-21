"""真实HTTP响应中断、员工停用和模型故障不能改变任务执行所有权。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from xuanpu_server.api import create_app
from xuanpu_server.errors import PermissionDeniedError
from xuanpu_server.tenancy import QuotaLimits
from xuanpu_server.work_tasks.models import WorkTaskInput


async def wait_for(predicate):
    """模型受控停留直到测试确认后台已开始。"""
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(0.02)


async def source_request(world):
    """本人文件已实际写入，交办只携带固定标识与版本。"""
    item = await world.platform.workspace.upload(world.office_user,'断线.txt',b'real source','text/plain','disconnect-source')
    return {'request_id':'disconnect-task','text':'整理纪要','input_files':[{'item_id':item['id'],'revision':1}]}


async def test_http_connection_loss_does_not_cancel_accepted_execution(office_world, step):
    """ASGI发送202时真实抛断线异常；独立执行仍保存两份真实成果。"""
    world, platform, who = office_world, office_world.platform, office_world.office_user
    gate = asyncio.Event()
    request = await source_request(world)
    manifest = {'kind':'meeting_minutes','artifacts':[
        {'path':'outputs/minutes.md','name':'纪要.md','role':'minutes','mime_type':'text/markdown'},
        {'path':'outputs/actions.json','name':'行动项.json','role':'actions','mime_type':'application/json'}]}
    world.models.script(step(calls=[
        ('m','file_write',{'path':'outputs/minutes.md','content':'# 真实纪要'}),
        ('a','file_write',{'path':'outputs/actions.json','content':'[]'}),
        ('f','file_write',{'path':'outputs/manifest.json','content':json.dumps(manifest)})]),
        step(text='根轮结束前保持运行',wait=gate))
    issued = await platform.issue_key(world.admin,tenant_id='acme',name='断线测试',
                                     role='tenant_member',act_as_users=True)
    body = json.dumps(request).encode()
    scope = {'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST',
        'scheme':'http','path':'/v1/work-tasks','raw_path':b'/v1/work-tasks','query_string':b'',
        'root_path':'','server':('test',80),'client':('test',123),'headers':[
            (b'authorization',('Bearer '+issued.key).encode()),(b'x-xuanpu-act-as',who.user_id.encode()),
            (b'content-type',b'application/json')]}
    statuses = []

    async def receive():
        """交办请求已完整到达，网络在响应阶段断开。"""
        return {'type':'http.request','body':body,'more_body':False}

    async def send(message):
        """已受理响应发不出去，模拟断线而不替换业务处理。"""
        if message['type'] == 'http.response.start':
            statuses.append(message['status'])
            raise ConnectionResetError('受控HTTP断连')

    try:
        with pytest.raises(ConnectionResetError):
            await create_app(platform)(scope,receive,send)
        assert statuses[0] == 202
        await wait_for(lambda:len(world.models.requests) >= 2)
        task = await platform.work_tasks.store.find_request(who,request['request_id'])
        assert task.status == 'running'
    finally:
        gate.set()
    await asyncio.wait_for(platform.task_registry.join(),15)
    current = await platform.work_tasks.store.get(who,task.task_id)
    assert current.status == 'succeeded' and len(current.artifacts) == 2
    assert not current.resources_pending


async def test_employee_disable_waits_for_owned_execution_cleanup(office_world, step):
    """停用监听同一执行者并清理容器，失效员工不能再查询内容。"""
    world, platform, who = office_world, office_world.platform, office_world.office_user
    world.models.script(step(text='处理中',wait=asyncio.Event()))
    task = await platform.work_tasks.accept(who,WorkTaskInput.model_validate(await source_request(world)))
    await wait_for(lambda:bool(world.models.requests))
    assert platform.runtimes.live_runtimes() == 1
    await platform.identity.disable_user('acme',who.user_id,'admin',world.admin,keep_an_admin=True)
    records = await platform.work_tasks.store.lifecycle_records('acme',who.user_id)
    assert records == []
    assert platform.runtimes.live_runtimes() == 0
    assert not platform.task_runner.inputs.directory(task).exists()
    with pytest.raises(PermissionDeniedError):
        await platform.work_tasks.store.get(who,task.task_id)


@pytest.mark.parametrize('failure',['model_timeout','budget'])
async def test_provider_timeout_or_budget_never_produces_success(office_world, failure):
    """供应商超时注入与真实配额拒绝都无成果，且执行资源确实退出。"""
    world, platform, who = office_world, office_world.platform, office_world.office_user
    if failure == 'budget':
        tenant = await platform.store.get_tenant('acme')
        await platform.store.update_tenant(replace(tenant,quota=QuotaLimits(max_daily_tokens=1)))
        await platform.store.add_usage('acme',day=datetime.now(UTC).date().isoformat(),
            input_tokens=1,output_tokens=0,total_tokens=1,turns=1)
    else:
        class TimeoutModels:
            """在已明确的供应商边界注入超时，不模拟业务成功。"""

            async def create(self, *args):
                """真实异常类型由任务执行器结算。"""
                raise TimeoutError('受控供应商超时')

        platform.runtimes._models = TimeoutModels()
    task = await platform.work_tasks.accept(who,WorkTaskInput.model_validate(await source_request(world)))
    await asyncio.wait_for(platform.task_registry.join(),15)
    current = await platform.work_tasks.store.get(who,task.task_id)
    assert current.status == 'failed' and current.error_code and not current.artifacts
    assert platform.runtimes.live_runtimes() == 0
    assert not platform.task_runner.inputs.directory(task).exists()

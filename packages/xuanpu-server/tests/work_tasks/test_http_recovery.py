"""真实HTTP无状态批准续接及scratch/MCP恢复拒绝。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from xuanpu_server.errors import ConflictError
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.platform import Platform
from xuanpu_server.runtime.sandbox import DockerSandboxProvider
from xuanpu_server.tenancy import Principal
from xuanpu_server.work_tasks.config import DurableExecutionConfig
from xuanpu_server.work_tasks.decisions import ToolApprovalDecision
from xuanpu_server.work_tasks.models import CapabilityTaskInput
from xuanpu_spec import pack_package


@pytest.fixture
async def http_world(identity_stores, world_opener, office_image):
    """HTTP侧车在平台对象之外，关闭重建平台不清空独立计数。"""
    counts = []
    async def receive(reader, writer):
        line = await reader.readline()
        await reader.readuntil(b'\r\n\r\n')
        counts.append(line.decode().split()[1])
        body = b'{"customer":"C-R2","state":"verified"}'
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                     +str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(receive, '127.0.0.1', 0)
    url = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
    control, identity = identity_stores
    config = DurableExecutionConfig(enabled=True, verified_text_bounds=True,
                                   stateless_http_tools=('api__counter__read',))
    provider = DockerSandboxProvider(image=office_image, memory_mb=512,
        docker_env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(Path.home())},
        labels={'xuanpu.test':'r2-http'})
    world = await world_opener(store=control, identity_store=identity, sandboxes=provider, durable_execution=config)
    root = world.package_dir/'http-recovery'
    (root/'skills/query').mkdir(parents=True)
    (root/'xuanpu.yaml').write_text('apiVersion: xuanpu/v1\nmetadata: {name: http-recovery, version: 1.0.0}\n'
        'spec:\n  skills: {entries: [query]}\n  policy: {approval: strict}\n  connectors:\n'
        '    - name: counter\n      transport: http\n      openapi: api.yaml\n      baseUrl: '+url+'\n')
    (root/'api.yaml').write_text('openapi: 3.0.3\ninfo: {title: counter}\npaths:\n  /read:\n'
        '    get: {operationId: read, summary: 读取合成客户}\n')
    (root/'skills/query/SKILL.md').write_text('---\nname: query\ndescription: 查询客户\nversion: 1.0.0\n'
        'type: composite\nentry: true\ntool_names: [api__counter__read, file_write]\n---\n查询客户。\n')
    await world.platform.packages.publish(world.admin, pack_package(root), activate=True)
    await identity.create_user(UserRecord(tenant_id='acme',user_id='usr_http',display_name='员工',created_at=1.))
    world.employee = Principal(role='tenant_member',tenant_id='acme',user_id='usr_http',key_id=None)
    world.counts = counts
    world.durable_config = config
    world.provider = provider
    try:
        async with server:
            yield world
    finally:
        await world.platform.close()


async def reopen(world):
    """同库同消息存储，创建全新的平台和执行owner。"""
    old = world.platform
    await old.close()
    world.platform = Platform(store=old.store, identity_store=old.identity.store,
        identity_settings=old.identity.settings, vault=old.vault, models=world.models,
        sandboxes=world.provider,data_dir=world.data_dir,objects=world.objects,root_key_hash='0'*64,
        instance_id='r2-http-reopened',durable_execution=world.durable_config)
    await world.platform.start()


@pytest.mark.parametrize('scratch', [False, True])
async def test_original_http_approval_preserved_and_scratch_blocks(http_world, step, scratch):
    """无状态HTTP沿用原session/thread/call和批准ID；文件依赖保持零外部调用。"""
    world = http_world
    if scratch:
        world.models.script(step(calls=[('scratch','file_write',{'path':'scratch/authority.txt','content':'local evidence'})]))
    world.models.script(step(calls=[('read-original','api__counter__read',{})]), step(text='客户信息已读取'))
    task = await world.platform.work_tasks.accept_capability(world.employee,
        CapabilityTaskInput.model_validate({'request_id':'http-cap','text':'查询客户',
            'capability':{'package':'http-recovery','entry':'query'}}))
    await asyncio.wait_for(world.platform.task_registry.join(),15)
    pending_task = await world.platform.work_tasks.store.get(world.employee, task.task_id)
    assert pending_task.status == 'waiting_user' and pending_task.pending, pending_task
    assert world.counts == []
    before = await world.platform.store.get_session('acme', pending_task.session_id)
    await reopen(world)
    current = await world.platform.work_tasks.store.get(world.employee, task.task_id)
    assert current.pending == pending_task.pending
    decision = ToolApprovalDecision.model_validate({'kind':'tool_approval','request_id':'original-approval',
        'state_version':current.state_version,
        'decisions':{entry['request_id']:{'granted':True} for entry in current.pending}})
    if scratch:
        with pytest.raises(ConflictError, match='block_resume'):
            await world.platform.work_tasks.decide(world.employee,task.task_id,decision)
        assert world.counts == []
    else:
        await world.platform.work_tasks.decide(world.employee,task.task_id,decision)
        await asyncio.wait_for(world.platform.task_registry.join(),15)
        result = await world.platform.work_tasks.store.get(world.employee,task.task_id)
        assert result.status == 'succeeded', result
        after = await world.platform.store.get_session('acme',result.session_id)
        assert after.session_id == before.session_id and after.thread_id == before.thread_id
        assert len(world.counts) == 1
        assert await world.platform.store.count_active_sessions('acme') == 0
        await world.platform.work_tasks.decide(world.employee, task.task_id, decision)
        assert len(world.counts) == 1


async def test_approval_handoff_survives_commit_before_launch(http_world, step, monkeypatch):
    """批准事务已提交但尚未启动执行时中断，原载荷可由持久派发补交。"""
    world = http_world
    world.models.script(step(calls=[('original-read','api__counter__read',{})]),step(text='已经读取客户'))
    task = await world.platform.work_tasks.accept_capability(world.employee,
        CapabilityTaskInput.model_validate({'request_id':'handoff','text':'查询客户',
            'capability':{'package':'http-recovery','entry':'query'}}))
    await asyncio.wait_for(world.platform.task_registry.join(),15)
    current = await world.platform.work_tasks.store.get(world.employee,task.task_id)
    decision = ToolApprovalDecision.model_validate({'kind':'tool_approval','request_id':'commit-then-loss',
        'state_version':current.state_version,'decisions':{entry['request_id']:{'granted':True} for entry in current.pending}})
    original = world.platform.task_registry._launch
    def stop(*args,**kwargs):
        """只在持久批准提交之后、调用Resume之前切断移交。"""
        raise OSError('受控移交中断')
    monkeypatch.setattr(world.platform.task_registry,'_launch',stop)
    with pytest.raises(OSError):
        await world.platform.work_tasks.decide(world.employee,task.task_id,decision)
    committed = await world.platform.work_tasks.store.get(world.employee,task.task_id)
    assert committed.pending_decision == decision.model_dump(mode='json')
    assert world.counts == []
    monkeypatch.setattr(world.platform.task_registry,'_launch',original)
    await reopen(world)
    await world.platform.task_registry.reconcile()
    await asyncio.wait_for(world.platform.task_registry.join(),15)
    result = await world.platform.work_tasks.store.get(world.employee,task.task_id)
    assert result.status == 'succeeded' and not result.pending_decision, result
    assert len(world.counts) == 1
    await world.platform.work_tasks.decide(world.employee,task.task_id,decision)
    assert len(world.counts) == 1

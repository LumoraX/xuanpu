"""完整办公任务、实际批准和独立HTTP计数器的未知写入验收。"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml
from xuanpu_server.work_tasks.config import DurableExecutionConfig
from xuanpu_server.work_tasks.decisions import ToolApprovalDecision
from xuanpu_server.work_tasks.models import WorkTaskInput
from xuanpu_spec import pack_package


@pytest.mark.parametrize('drop_reply',[True,False])
async def test_full_task_remote_write_remains_unknown_and_never_reissued(recovery_world,step,drop_reply):
    """下游已应用后丢响应，或缺业务权威验证器时，重建都不能重发。"""
    h = recovery_world
    applied = []
    async def remote(reader,writer):
        header = await reader.readuntil(b'\r\n\r\n')
        length = next((int(line.split(b':',1)[1]) for line in header.split(b'\r\n')
                       if line.lower().startswith(b'content-length:')),0)
        body = await reader.readexactly(length) if length else b''
        applied.append(body)
        if not drop_reply:
            data = b'{"applied":true,"id":"synthetic-1"}'
            writer.write(b'HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: '
                +str(len(data)).encode()+b'\r\nConnection: close\r\n\r\n'+data)
            await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(remote,'127.0.0.1',0)
    url = 'http://127.0.0.1:'+str(server.sockets[0].getsockname()[1])
    config = DurableExecutionConfig(enabled=True,verified_text_bounds=True,
                                   stateless_http_tools=('api__counter__apply',))
    h.platform.work_tasks.durable = config
    h.platform.work_tasks.store.durable_config = config
    h.platform.task_registry.durable.config = config
    root = h.directory/'external-office'
    shutil.copytree(Path(__file__).resolve().parents[4]/'examples/office-assistant',root)
    manifest = yaml.safe_load((root/'xuanpu.yaml').read_text())
    manifest['metadata']['version'] = '1.0.3'
    manifest['spec']['connectors'] = [{'name':'counter','transport':'http','openapi':'api.yaml','baseUrl':url}]
    (root/'xuanpu.yaml').write_text(yaml.safe_dump(manifest,allow_unicode=True))
    (root/'api.yaml').write_text('openapi: 3.0.3\ninfo: {title: counter}\npaths:\n  /apply:\n'
        '    post: {operationId: apply, summary: 合成写操作}\n')
    skill = root/'skills/office/SKILL.md'
    skill.write_text(skill.read_text().replace('tool_names: [','tool_names: [api__counter__apply, '))
    await h.platform.packages.publish(h.world.admin,pack_package(root),activate=True)
    source = await h.platform.workspace.upload(h.who,'材料.txt',b'synthetic source','text/plain','external-source')
    h.world.models.script(step(calls=[('original-write','api__counter__apply',{})]),step(text='不得执行后续模型'))
    async with server:
        task = await h.platform.work_tasks.accept(h.who,WorkTaskInput.model_validate({'request_id':'external-task',
            'text':'执行本人确认的合成操作','input_files':[{'item_id':source['id'],'revision':1}]}))
        h.task = task
        await asyncio.wait_for(h.platform.task_registry.join(),20)
        current = await h.read_task()
        assert current.status == 'waiting_user' and applied == [],current
        decision = ToolApprovalDecision.model_validate({'kind':'tool_approval','request_id':'approve-write-once',
            'state_version':current.state_version,'decisions':{entry['request_id']:{'granted':True} for entry in current.pending}})
        await h.platform.work_tasks.decide(h.who,task.task_id,decision)
        await asyncio.wait_for(h.platform.task_registry.join(),20)
        current = await h.read_task()
        assert current.status == 'outcome_unknown' and current.unverified_write,current
        assert len(applied) == 1 and len(h.world.models.requests) == 1
        await h.platform.close()
        await h.reopen()
        result = await h.read_task()
        assert result.status == 'outcome_unknown' and not result.resources_pending
        await h.platform.work_tasks.decide(h.who,task.task_id,decision)
        await h.platform.task_registry.reconcile()
        assert len(applied) == 1 and len(h.world.models.requests) == 1

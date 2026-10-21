"""独立进程SIGKILL后，实际PostgreSQL状态与Docker孤儿资源得到结算。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from xuanpu_server.platform import Platform
from xuanpu_server.work_tasks.models import WorkTaskInput


async def docker(*args):
    """只检查或清理本用例已知容器，不扫描和影响其他运行资源。"""
    process = await asyncio.create_subprocess_exec('docker',*args,stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    return process.returncode, stdout, stderr


@pytest.mark.parametrize('identity_stores', ['postgres'], indirect=True)
@pytest.mark.parametrize('mode',['running','waiting_user','waiting_cancel'])
async def test_sigkill_recovery_stops_known_container_without_replaying(office_world, postgres_dsn, tmp_path, mode):
    """实际强杀拥有执行的宿主，再启同数据目录；只能中断，不能自动续跑。"""
    world = office_world
    platform, who = world.platform, world.office_user
    await platform.task_registry.close()
    platform.work_tasks.executor = None
    source = await platform.workspace.upload(who,'重启材料.txt',b'preserve source','text/plain','restart-input')
    task = await platform.work_tasks.accept(who, WorkTaskInput.model_validate({'request_id':'restart-task',
        'text':'整理会议','input_files':[{'item_id':source['id'],'revision':1}]}))
    provider = platform.runtimes._sandboxes
    await platform.close()
    ready, config = tmp_path/'ready.json', tmp_path/'restart-private.json'
    config.write_text(json.dumps({'dsn':postgres_dsn,'schema':platform.store._namespace.schema,
        'data':str(world.data_dir),'objects':str(world.objects_dir),'task':task.task_id,'user':who.user_id,
        'image':'xuanpu-office:r1-20261021','ready':str(ready),
        'mode':'running' if mode == 'running' else 'waiting_user'}));config.chmod(0o600)
    helper = Path(__file__).resolve().parents[1]/'data/r1_restart_worker.py'
    process = await asyncio.create_subprocess_exec(sys.executable,str(helper),str(config),
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    name, restarted = None, None
    try:
        async with asyncio.timeout(30):
            while not ready.exists():
                if process.returncode is not None:
                    _, error = await process.communicate()
                    pytest.fail(error.decode()[-4000:])
                await asyncio.sleep(0.05)
        name = json.loads(ready.read_text())['container']
        process.kill();await process.wait()
        assert process.returncode < 0
        assert (await docker('inspect','--format','{{.State.Running}}',name))[:2] == (0,b'true\n')
        samples = world.models.requests
        class ExplicitDecisionModels:
            """恢复不自动采样，本人显式决定后才允许可控模型解释拒绝。"""

            async def create(self, *args):
                """客户端构造并非采样；实际请求由models.requests记录。"""
                return world.models

        restarted = Platform(store=platform.store, identity_store=platform.identity.store,
            identity_settings=platform.identity.settings, vault=platform.vault, models=ExplicitDecisionModels(),
            sandboxes=provider, data_dir=world.data_dir, root_key_hash='0'*64, objects=world.objects,
            instance_id='after-crash')
        await restarted.start()
        recovered = await restarted.work_tasks.store.get(who, task.task_id)
        assert recovered.status == ('interrupted' if mode == 'running' else 'waiting_user')
        session = await restarted.store.get_session('acme',recovered.session_id)
        assert session.owner is None
        assert not recovered.resources_pending and recovered.resources_recovered
        assert (await docker('inspect',name))[0] != 0
        directory = restarted.task_runner.inputs.directory(task)
        assert not directory.exists() or list(directory.iterdir()) == []
        assert (await restarted.workspace.download(who,source['id']))[0] == b'preserve source'
        assert not samples
        if mode == 'waiting_cancel':
            cancelled = await restarted.work_tasks.cancel(who,task.task_id)
            assert cancelled.status == 'cancelled'
            assert await restarted.store.count_active_sessions('acme') == 0
            assert not restarted.task_runner.inputs.directory(task).exists()
            assert not samples
        if mode == 'waiting_user':
            from xuanpu_server.work_tasks.decisions import ToolApprovalDecision
            await restarted.work_tasks.decide(who,task.task_id,ToolApprovalDecision.model_validate({
                'kind':'tool_approval','request_id':'reject-after-restart','state_version':recovered.state_version,
                'decisions':{entry['request_id']:{'granted':False} for entry in recovered.pending}}))
            await asyncio.wait_for(restarted.task_registry.join(),15)
            decided = await restarted.work_tasks.store.get(who,task.task_id)
            assert decided.status == 'failed' and not decided.artifacts
            assert len(samples) == 1
            markers = list(world.data_dir.glob('workspaces/**/scratch/started'))
            assert not markers
    finally:
        if process.returncode is None:
            process.kill();await process.wait()
        _, errors = await process.communicate()
        (tmp_path/'worker.log').write_bytes(errors)
        if restarted is not None:
            await restarted.close()
        if name is not None:
            await docker('rm','--force',name)

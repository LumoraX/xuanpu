"""被验收父进程强制终止的独立工作进程，仅接受临时测试配置。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import asyncpg
from taifeng_stores import SqlNamespace
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.identity.config import IdentityConfig
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.identity.service import IdentitySettings
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.platform import Platform
from xuanpu_server.runtime.sandbox import DockerSandboxProvider, _container_name
from xuanpu_server.secrets import EncryptedVault, generate_master_key
from xuanpu_server.tenancy import Principal
from xuanpu_server.work_tasks.decisions import ToolApprovalDecision


async def main() -> None:
    """命令实际开始后报告已知容器名；等待SIGKILL，不主动正常退出。"""
    config = json.loads(Path(sys.argv[1]).read_text())
    fixture_path = Path(__file__).resolve().parents[1] / 'conftest.py'
    spec = importlib.util.spec_from_file_location('restart_fixture', fixture_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    pool = await asyncpg.create_pool(config['dsn'], min_size=1, max_size=8)
    namespace = SqlNamespace(schema=config['schema'], prefix='xp_')
    control = PostgresControlPlane(pool, namespace=namespace)
    models = module.ScriptedModels()
    models.script(module.Step(calls=[('long','shell_exec',{'command':'mkdir -p scratch; touch scratch/started; sleep 600'})]),
                  module.Step(text='命令意外结束'))
    platform = Platform(store=control, identity_store=PostgresIdentityStore(pool, namespace=namespace),
        identity_settings=IdentitySettings.from_config(None, IdentityConfig()),
        vault=EncryptedVault(control, keys={'k1':generate_master_key()}, primary='k1'), models=models,
        sandboxes=DockerSandboxProvider(image=config['image'], memory_mb=512,
            docker_env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(Path.home())},
            labels={'xuanpu.test':'r1-restart'}), data_dir=Path(config['data']),
        objects=LocalObjectStore(Path(config['objects'])), root_key_hash='0'*64, instance_id='inst-test')
    await platform.start()
    who = Principal(role='tenant_member', tenant_id='acme', user_id=config['user'], key_id=None)
    while True:
        task = await platform.work_tasks.store.get(who, config['task'])
        Path(config['ready']+'.trace').write_text(json.dumps({'status':task.status,'pending':task.pending,
            'active':len(platform.task_registry._active),'contexts':len(platform.task_runner._contexts)}))
        if task.status == 'waiting_user' and task.pending:
            if config.get('mode') == 'waiting_user':
                context = next(iter(platform.task_runner._contexts.values()))
                Path(config['ready']).write_text(json.dumps({'container':_container_name(context.runtime.sandbox.workspace),
                                                            'task':task.task_id,'input':str(context.root)}))
                await asyncio.Event().wait()
            assert all(entry.get('detail',{}).get('scope') == 'shell_exec' for entry in task.pending)
            await platform.work_tasks.decide(who, task.task_id, ToolApprovalDecision.model_validate({
                'kind':'tool_approval','request_id':'restart-local-shell','state_version':task.state_version,
                'decisions':{entry['request_id']:{'granted':True} for entry in task.pending}}))
        contexts = list(platform.task_runner._contexts.values())
        if contexts and (contexts[0].runtime.sandbox.workspace/'scratch/started').exists():
            Path(config['ready']).write_text(json.dumps({'container':_container_name(
                contexts[0].runtime.sandbox.workspace), 'task':task.task_id,'input':str(contexts[0].root)}))
            await asyncio.Event().wait()
        if task.status in {'failed','succeeded','interrupted','outcome_unknown','cancelled'}:
            raise AssertionError(task.model_dump())
        await asyncio.sleep(0.05)


if __name__ == '__main__':
    asyncio.run(main())

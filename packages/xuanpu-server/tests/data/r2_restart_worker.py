"""真实R2执行子进程：到实际阶段暂停，由父进程SIGKILL。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import signal
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
from xuanpu_server.work_tasks.config import DurableExecutionConfig


async def write_ready(platform,who,config,models,phase):
    """从实际阶段读取证据，不在测试准备器中构造完成结论。"""
    current=await platform.work_tasks.store.get(who,config['task'])
    contexts=list(platform.task_runner._contexts.values())
    Path(config['ready']).write_text(json.dumps({'phase':phase,'task':current.task_id,
        'attempt_no':current.attempt_no,'session_id':current.session_id,
        'inputs':[entry.model_dump() for entry in current.inputs],'model_calls':len(models.requests),
        'container':_container_name(contexts[0].runtime.sandbox.workspace) if contexts else None}))


async def main():
    """故障注入只包装平台检查点，实际事务、容器与模型协议均照常执行。"""
    config = json.loads(Path(sys.argv[1]).read_text())
    spec = importlib.util.spec_from_file_location('r2_fixture', Path(__file__).resolve().parents[1]/'conftest.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    pool = await asyncpg.create_pool(config['dsn'], min_size=1, max_size=8)
    namespace = SqlNamespace(schema=config['schema'], prefix='xp_')
    control = PostgresControlPlane(pool, namespace=namespace)
    models = module.ScriptedModels()
    steps = []
    for value in config['steps']:
        value = dict(value)
        if value.pop('hold', False):
            value['wait'] = asyncio.Event()
        steps.append(module.Step(**value))
    models.script(*steps)
    provider = DockerSandboxProvider(user=module.docker_test_user(), image=config['image'], memory_mb=512,
        docker_env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(Path.home())},
        labels={'xuanpu.test':'r2-restart'})
    platform = Platform(store=control, identity_store=PostgresIdentityStore(pool, namespace=namespace),
        identity_settings=IdentitySettings.from_config(None, IdentityConfig()),
        vault=EncryptedVault(control, keys={'k1':generate_master_key()}, primary='k1'), models=models,
        sandboxes=provider, data_dir=Path(config['data']), objects=LocalObjectStore(Path(config['objects'])),
        root_key_hash='0'*64, instance_id='r2-crash-owner',
        durable_execution=DurableExecutionConfig(enabled=True, verified_text_bounds=True,
            max_attempts=config.get('max_attempts', 3)))
    who = Principal(role='tenant_member', tenant_id='acme', user_id=config['user'], key_id=None)
    checkpoint = platform.task_registry.durable.execution.checkpoint
    async def pause(lease, phase, evidence=None, **kwargs):
        """提交真实检查点之后暂停，不伪造任何已完成状态。"""
        await checkpoint(lease, phase, evidence, **kwargs)
        target = phase
        if phase == 'artifacts_staged' and evidence and 'uploaded' in evidence:
            target = 'artifact_uploaded:' + str(evidence['uploaded'])
        if target == config['phase']:
            # 确认模型请求已真实进入协议，再提供强杀点。
            if target == 'executing':
                async with asyncio.timeout(10):
                    while not models.requests:
                        await asyncio.sleep(.01)
            await write_ready(platform,who,config,models,target)
            await asyncio.Event().wait()
    platform.task_registry.durable.execution.checkpoint = pause
    stopping = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stopping.set)
    if config['phase']=='accepted':
        current=await platform.work_tasks.store.get(who,config['task'])
        Path(config['ready']).write_text(json.dumps({'phase':'accepted','task':current.task_id,
            'attempt_no':current.attempt_no,'inputs':[entry.model_dump() for entry in current.inputs],
            'model_calls':0,'session_id':None,'container':None}))
        await asyncio.Event().wait()
    await platform.start()
    while not stopping.is_set():
        if config['phase']=='script_running':
            contexts=list(platform.task_runner._contexts.values())
            if contexts and (contexts[0].runtime.sandbox.workspace/'scratch/hold-ready').exists():
                await write_ready(platform,who,config,models,'script_running')
                await asyncio.Event().wait()
        task = await platform.work_tasks.store.get(who, config['task'])
        if task.status in {'failed','succeeded','interrupted','outcome_unknown','cancelled'}:
            raise AssertionError(task.model_dump())
        await asyncio.sleep(.05)
    await platform.close()
    await pool.close()


if __name__ == '__main__':
    asyncio.run(main())

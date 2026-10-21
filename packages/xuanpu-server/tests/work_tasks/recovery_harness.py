"""统一真实双后端工作区与独立进程故障harness。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from xuanpu_server.platform import Platform
from xuanpu_server.work_tasks.config import DurableExecutionConfig
from xuanpu_server.work_tasks.models import WorkTaskInput


class RecoveryWorld:
    """故障进程和HTTP侧车不共享执行所有权；崩溃真的发送SIGKILL。"""

    def __init__(self, world, directory, dsn=None):
        """普通契约覆盖双后端，进程强杀要求真实PostgreSQL。"""
        self.world, self.directory, self.dsn = world, directory, dsn
        self.platform = world.platform
        self.who = world.office_user
        self.task = None
        self.child = None
        self.ready = None
        self.old_calls = 0
        self.steps = []
        self.origin_key_id = None

    @property
    def model_calls(self):
        """只统计真实stream请求，不能把模型工厂构造算调用。"""
        return self.old_calls + len(self.world.models.requests)

    async def accept_office(self, *, request_id='r2-office', output_kind='table_summary', origin='cookie'):
        """材料、受理、授权和派发都走真实服务。"""
        await self.platform.task_registry.close()
        self.platform.work_tasks.executor = None
        content, mime = ('部门,金额\nA,10.20\nA,2.30\nB,3.00\n'.encode(), 'text/csv') if output_kind == 'table_summary' else (b'meeting source', 'text/plain')
        file = await self.platform.workspace.upload(self.who, '固定材料', content, mime, 'r2-source')
        actor = self.who
        if origin == 'act_as_key':
            from xuanpu_server.tenancy import ApiKeyRecord, generate_api_key

            _,identifier,digest = generate_api_key()
            self.origin_key_id = identifier
            await self.platform.store.create_api_key(ApiKeyRecord(key_id=identifier,tenant_id='acme',
                name='R2合成验收来源',role='tenant_member',secret_hash=digest,created_at=time.time(),can_act_as_user=True))
            actor = replace(self.who,key_id=identifier)
        self.task = await self.platform.work_tasks.accept(actor, WorkTaskInput.model_validate({
            'request_id':request_id, 'text':'按部门汇总金额，保留核对证据',
            'input_files':[{'item_id':file['id'],'revision':1}]}))
        return self.task

    async def run_until(self, phase):
        """运行独立真实平台，直到已提交的实际检查点。"""
        if self.dsn is None:
            raise ValueError('SIGKILL故障harness需要真实PostgreSQL')
        await self.platform.close()
        path, ready = self.directory/'r2-worker.json', self.directory/'r2-ready.json'
        path.write_text(json.dumps({'dsn':self.dsn,'schema':self.platform.store._namespace.schema,
            'data':str(self.world.data_dir),'objects':str(self.world.objects_dir), 'task':self.task.task_id,
            'user':self.who.user_id,'image':'xuanpu-office:r1-20261021', 'ready':str(ready),
            'phase':phase,'steps':self.steps}))
        path.chmod(0o600)
        self.child = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).resolve().parents[1]/'data/r2_restart_worker.py'), str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async with asyncio.timeout(45):
            while not ready.exists():
                if self.child.returncode is not None:
                    _, error = await self.child.communicate()
                    raise AssertionError(error.decode()[-6000:])
                await asyncio.sleep(.05)
        self.ready = json.loads(ready.read_text())
        self.old_calls = self.ready['model_calls']
        return self.ready

    async def crash(self):
        """独立进程强杀，绝不把正常close冒充崩溃。"""
        self.child.kill()
        await self.child.wait()
        assert self.child.returncode < 0
        _, errors = await self.child.communicate()
        (self.directory/'r2-worker.log').write_bytes(errors)

    async def stop(self):
        """向真实子进程发送SIGTERM并等待完整平台退出。"""
        self.child.terminate()
        async with asyncio.timeout(30):
            await self.child.wait()
        _,errors = await self.child.communicate()
        (self.directory/'r2-worker.log').write_bytes(errors)
        assert self.child.returncode == 0,errors.decode()[-5000:]

    async def reopen(self, *, advance_seconds=0):
        """同库、同对象和同数据目录，新进程所有者重新承担执行责任。"""
        old = self.platform
        self.platform = Platform(store=old.store, identity_store=old.identity.store,
            identity_settings=old.identity.settings, vault=old.vault, models=self.world.models,
            sandboxes=old.runtimes._sandboxes, data_dir=self.world.data_dir, objects=self.world.objects,
            root_key_hash='0'*64, instance_id='r2-after-crash',
            now=(lambda: time.time()+advance_seconds) if advance_seconds else time.time,
            durable_execution=DurableExecutionConfig(enabled=True, verified_text_bounds=True),
            task_activity=old.store.workspace_store._activity_config)
        self.world.platform = self.platform
        await self.platform.start()

    async def revoke_origin_key(self):
        """调用真实key存储吊销，不能改Principal假装撤权。"""
        assert self.origin_key_id is not None
        await self.platform.store.revoke_api_key('acme',self.origin_key_id,at=time.time())

    async def read_dispatch(self):
        """内部验收读取本人派发状态。"""
        return await self.platform.task_registry.durable.execution.get(self.who,self.task.task_id)

    async def read_task(self):
        """只从真实持久任务读取业务结论。"""
        return await self.platform.work_tasks.store.get(self.who, self.task.task_id)

    async def drain(self):
        """有界等待，派发和执行仍由唯一Registry拥有。"""
        async with asyncio.timeout(30):
            for _ in range(100):
                await self.platform.task_registry.reconcile()
                await self.platform.task_registry.join()
                current = await self.read_task()
                if current.status in {'succeeded','failed','outcome_unknown','interrupted','cancelled','waiting_user'}:
                    return current
                await asyncio.sleep(.05)
            raise AssertionError(await self.read_task())

    async def counts(self):
        """文件数读取本人正本；调用次数来自协议边界。"""
        files = await self.platform.workspace.list(self.who, kind='file')
        return {'artifact_files':len(files)-1,'model_calls':self.model_calls}

    async def notifications(self,task_id):
        """从真实通知存储补读本人队列，不维护测试替身列表。"""
        records=await self.platform.store.reminder_store.notifications(self.who.tenant_id,self.who.user_id,0,100)
        return [n for n in records if n.work_task_id==task_id]

    async def reconcile_again(self):
        """驱动原唯一执行器再次补读，证明事件与通知幂等。"""
        await self.platform.task_registry.reconcile()
        await self.platform.task_registry.join()

    async def close(self):
        """失败用例也清理明确属于自己的子进程和平台资源。"""
        if self.child is not None and self.child.returncode is None:
            await self.crash()
        await self.platform.close()

"""显式启用的真实网关验收，凭据只读本机私有env，不进入证据。"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from xuanpu_server.runtime.models import GatewayConfig, GatewayModelClientFactory, ModelProfile
from xuanpu_server.work_tasks.models import WorkTaskInput

MODEL_ENV = os.environ.get('XUANPU_R2_REAL_MODEL_ENV')
pytestmark = [pytest.mark.skipif(not MODEL_ENV, reason='真实模型验收需明确提供私有env路径'),
              pytest.mark.parametrize('identity_stores', ['postgres'], indirect=True)]


class RecordedFactory:
    """记录公开协议请求及原始模型事件，绝不记录认证头。"""
    def __init__(self, factory):
        self.factory, self.calls = factory, []
    async def create(self, tenant, tier):
        inner = await self.factory.create(tenant, tier)
        records = self.calls
        class Client:
            """测试观察器保持原模型会话协议。"""
            capabilities = getattr(inner,'capabilities',None)
            def session(self, **kwargs):
                session = inner.session(**kwargs)
                class Session:
                    """请求计数在实际stream，网络回执保留原始用量。"""
                    async def __aenter__(self):
                        await session.__aenter__()
                        return self
                    async def __aexit__(self,*args):
                        await session.__aexit__(*args)
                    async def stream(self, request):
                        record = {'request':request.model_dump(mode='json'),'events':[]}
                        records.append(record)
                        async for event in session.stream(request):
                            record['events'].append({'kind':event.kind,'data':event.data})
                            yield event
                return Session()
        return Client()


def xlsx_bytes():
    """复用已独立生成的验收常量，不调用办公包计算器。"""
    path = Path(__file__).resolve().parents[4]/'scripts/acceptance/r1_office.py'
    spec = importlib.util.spec_from_file_location('r2_independent_constants',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.constant_xlsx()


@pytest.mark.parametrize('format', ['csv','xlsx'])
async def test_real_model_frozen_upload_recovery(recovery_world, monkeypatch, format):
    """真实模型产生真实脚本成果；半上传停止后不再采样即可补齐四文件。"""
    env = dotenv_values(MODEL_ENV)
    assert env.get('LLM_BOOTSTRAP_PROTOCOL') in {'chat','responses','codex_responses'}
    harness = recovery_world
    platform, who = harness.platform, harness.who
    models = RecordedFactory(GatewayModelClientFactory(GatewayConfig(
        base_url=env['LLM_BOOTSTRAP_BASE_URL'], api_key=env['LLM_BOOTSTRAP_API_KEY'],
        protocol=env['LLM_BOOTSTRAP_PROTOCOL'], profiles={tier:ModelProfile(env['LLM_BOOTSTRAP_MODEL'])
        for tier in ('fast','balanced','deep')}),platform.vault))
    platform.runtimes._models = models
    paused = asyncio.Event()
    checkpoint = platform.task_registry.durable.execution.checkpoint
    async def pause(lease, phase, evidence=None, **kwargs):
        """第二份文件真实上传并提交后，暂停唯一执行者以验证正常重建。"""
        await checkpoint(lease,phase,evidence,**kwargs)
        if phase == 'artifacts_staged' and evidence and evidence.get('uploaded') == 2:
            paused.set()
            await asyncio.Event().wait()
    monkeypatch.setattr(platform.task_registry.durable.execution,'checkpoint',pause)
    if format == 'csv':
        data,mime = '部门,金额\nA,10.20\nA,2.30\nB,3.00\n'.encode(),'text/csv'
    else:
        data,mime = xlsx_bytes(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    file = await platform.workspace.upload(who,'真实模型合成材料.'+format,data,mime,'real-source')
    task = await platform.work_tasks.accept(who,WorkTaskInput.model_validate({'request_id':'real-table',
        'text':'请按部门汇总金额。分组列明确为“部门”，金额列明确为“金额”；无需追问列名。请实际执行表格汇总脚本，交付可下载汇总CSV、XLSX、报告和来源行证据。',
        'input_files':[{'item_id':file['id'],'revision':1}]}))
    harness.task = task
    output = Path(os.environ['XUANPU_R2_EVIDENCE_DIR'])/format
    output.mkdir(parents=True,exist_ok=True)
    try:
        async with asyncio.timeout(240):
            while not paused.is_set():
                current = await platform.work_tasks.store.get(who,task.task_id)
                if current.status in {'failed','outcome_unknown','interrupted','cancelled','waiting_user'}:
                    raise AssertionError({'status':current.status,'error':current.error_code,
                        'dispatch':(await platform.task_registry.durable.execution.get(who,task.task_id)).model_dump()})
                await asyncio.sleep(.1)
        calls = len(models.calls)
        assert calls > 0 and all(call['request']['max_output_tokens'] == 4096 for call in models.calls)
        await platform.close()
        paused_task = await platform.work_tasks.store.get(who,task.task_id)
        assert paused_task.status == 'running'
        await harness.reopen()
        current = await harness.drain()
        assert current.status == 'succeeded' and len(current.artifacts) == 4, current
        assert len(models.calls) == calls and not harness.world.models.requests
        files = {}
        for artifact in current.artifacts:
            content = (await harness.platform.workspace.download(who,artifact.item_id))[0]
            assert hashlib.sha256(content).hexdigest() == artifact.sha256
            (output/artifact.name).write_bytes(content)
            files[artifact.role] = content
        evidence = json.loads(files['evidence'])
        assert evidence['totals'][0]['amount'] == '15.50'
        assert evidence['record_count'] == 3
        usage = await platform.store.list_usage('acme',since='2000',until='2099')
        (output/'assessment.json').write_text(json.dumps({'model':env['LLM_BOOTSTRAP_MODEL'],
            'database':'real PostgreSQL', 'executor':'Linux Docker', 'format':format,'total':'15.50',
            'artifact_count':4, 'model_calls_before':calls,'model_calls_after':len(models.calls),
            'attempt_no':current.attempt_no,'input_sha256':hashlib.sha256(data).hexdigest(),
            'usage':[{'input_tokens':u.input_tokens,'output_tokens':u.output_tokens,'total_tokens':u.total_tokens} for u in usage],
            'task':current.model_dump(mode='json',exclude={'process_owner','grant_id','staging_objects','frozen_staging'})},
            ensure_ascii=False,indent=2))
    finally:
        (output/'raw-model.json').write_text(json.dumps(models.calls,ensure_ascii=False,indent=2))

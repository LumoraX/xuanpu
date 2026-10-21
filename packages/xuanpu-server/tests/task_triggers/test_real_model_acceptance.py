"""显式启用的真实模型触发验收，原始协议与成果保存在忽略目录。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from xuanpu_server.runtime.models import GatewayConfig, GatewayModelClientFactory, ModelProfile
from xuanpu_server.work_tasks.registry import TaskRegistry

from ..work_tasks.test_real_model_acceptance import RecordedFactory
from .conftest import request_for

MODEL_ENV=os.environ.get('XUANPU_R2_REAL_MODEL_ENV')
pytestmark=[pytest.mark.skipif(not MODEL_ENV,reason='真实网关验收须显式提供私有env路径'),
            pytest.mark.parametrize('identity_stores',['postgres'],indirect=True)]


@pytest.mark.parametrize('mode',['time','event'])
async def test_real_gateway_trigger_delivers_verified_total_once(trigger_world,mode):
    """上海定时和新版本事件各产生唯一真实Linux成果，重复投递不再采样。"""
    w=trigger_world
    env=dotenv_values(MODEL_ENV)
    platform=w.platform
    models=RecordedFactory(GatewayModelClientFactory(GatewayConfig(base_url=env['LLM_BOOTSTRAP_BASE_URL'],
        api_key=env['LLM_BOOTSTRAP_API_KEY'],protocol=env['LLM_BOOTSTRAP_PROTOCOL'],
        profiles={tier:ModelProfile(env['LLM_BOOTSTRAP_MODEL']) for tier in ('fast','balanced','deep')}),platform.vault))
    platform.runtimes._models=models
    service=platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.now=lambda:w.now
    body=await request_for(w)
    body['template']['text']='请按部门汇总金额，分组列为部门、金额列为金额，没有币种列。实际执行表格汇总脚本，交付CSV、XLSX、报告和来源行证据。'
    source=body['template']['input_files'][0]['item_id']
    if mode=='event':
        body.update(input_policy='latest_at_occurrence',trigger={'kind':'event',
            'event_type':'workspace.file_revision_committed','subject_id':source})
    record=await service.create(w.who,body)
    old=platform.task_registry
    registry=TaskRegistry(service.tasks.store,platform.task_runner,process_owner=platform.instance_id,
        now=lambda:w.now,durable=service.tasks.durable,cleanup=old.cleanup,recover=old.recover,actions=old.actions)
    platform.task_registry=registry
    service.tasks.executor=registry
    platform.task_runner.artifacts.now=lambda:w.now
    platform.task_runner.artifacts.staging.now=lambda:w.now
    platform.task_runner.proposals.now=lambda:w.now
    await registry.open(scan=False)
    output=Path(os.environ['XUANPU_R2_EVIDENCE_DIR'])/mode
    output.mkdir(parents=True,exist_ok=True)
    try:
        if mode=='time':
            w.now+=60
            await platform.trigger_worker.tick_time()
        else:
            await platform.workspace.replace_content(w.who,source,'部门,金额\nA,12.50\nB,3.00\n'.encode(),'text/csv',1,'real-event-v2')
            await platform.trigger_worker.tick_events()
        await asyncio.wait_for(registry.join(),240)
        tasks=await service.tasks.store.list(w.who)
        assert len(tasks)==1 and tasks[0].status=='succeeded',tasks
        task=tasks[0]
        assert len(task.artifacts)==4 and task.attempt_no==1
        files={}
        for artifact in task.artifacts:
            content=(await platform.workspace.download(w.who,artifact.item_id))[0]
            assert hashlib.sha256(content).hexdigest()==artifact.sha256
            (output/artifact.name).write_bytes(content)
            files[artifact.role]=content
        evidence=json.loads(files['evidence'])
        assert evidence['totals'][0]['amount']=='15.50' and evidence['record_count']==2
        before=len(models.calls)
        assert before>0 and all(call['request']['max_output_tokens']==4096 for call in models.calls)
        await platform.trigger_worker.tick_time()
        for _ in range(2):await platform.trigger_worker.tick_events()
        await registry.join()
        assert len(await service.tasks.store.list(w.who))==1 and len(models.calls)==before
        occurrence=(await service.occurrences(w.who,record.definition_id))[0]
        assert occurrence.input_files[0].revision==(1 if mode=='time' else 2)
        (output/'assessment.json').write_text(json.dumps({'mode':mode,'model':env['LLM_BOOTSTRAP_MODEL'],
            'database':'PostgreSQL','executor':'Linux Docker','occurrence':occurrence.model_dump(mode='json'),
            'verified_total':'15.50','record_count':2,'artifact_count':4,'attempt_no':task.attempt_no,
            'model_calls':before,'calls_after_replay':len(models.calls)},ensure_ascii=False,indent=2))
    finally:
        (output/'raw-model.json').write_text(json.dumps(models.calls,ensure_ascii=False,indent=2))

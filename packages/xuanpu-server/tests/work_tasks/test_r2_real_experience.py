"""R2-C真实模型纪要和无附件只读任务，保留原始产物与独立事实评估。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from dotenv import dotenv_values
from xuanpu_server.runtime.models import GatewayConfig, GatewayModelClientFactory, ModelProfile
from xuanpu_server.work_tasks.decisions import ToolApprovalDecision
from xuanpu_server.work_tasks.models import CapabilityTaskInput, WorkTaskInput

from .test_http_recovery import http_world  # noqa: F401
from .test_real_model_acceptance import RecordedFactory

MODEL_ENV=os.environ.get('XUANPU_R2_REAL_MODEL_ENV')
pytestmark=[pytest.mark.skipif(not MODEL_ENV,reason='真实模型需明确指定私有env路径'),
            pytest.mark.parametrize('identity_stores',['postgres'],indirect=True)]


def real_factory(platform):
    """只读私有配置，原始协议观察器不记录认证头。"""
    env=dotenv_values(MODEL_ENV)
    return RecordedFactory(GatewayModelClientFactory(GatewayConfig(base_url=env['LLM_BOOTSTRAP_BASE_URL'],
        api_key=env['LLM_BOOTSTRAP_API_KEY'],protocol=env['LLM_BOOTSTRAP_PROTOCOL'],
        profiles={tier:ModelProfile(env['LLM_BOOTSTRAP_MODEL']) for tier in ('fast','balanced','deep')}),platform.vault))


def save_evidence(label,models,assessment):
    """每轮独立目录，不覆盖首次失败和原始模型报告。"""
    root=Path(os.environ['XUANPU_R2_EVIDENCE_DIR'])/label
    root.mkdir(parents=True,exist_ok=False)
    (root/'raw-model.json').write_text(json.dumps(models.calls,ensure_ascii=False,indent=2))
    (root/'assessment.json').write_text(json.dumps(assessment,ensure_ascii=False,indent=2))


async def test_real_meeting_keeps_exact_quotes_and_unresolved_deadline(recovery_world):
    """未确认前只保存纪要和候选；明确和未决日期不混淆。"""
    h=recovery_world;p=h.platform;models=real_factory(p);p.runtimes._models=models
    today=datetime.now(ZoneInfo('Asia/Shanghai')).date();due=today+timedelta(days=3)
    text=f'会议日期：{today}。时区：Asia/Shanghai。\n已经决定：我（当前账号本人）负责整理需求方案，截止{due} 17:00。\n我还负责归档资料，但截止时间尚未确定，必须保持未知。\n其他同事李四负责复核，不要替他创建工作。'
    source=await p.workspace.upload(h.who,'R2合成会议.txt',text.encode(),'text/plain','real-meeting-source')
    assessment={'result':'failed','native_records_before_confirmation':0}
    try:
        task=await p.work_tasks.accept(h.who,WorkTaskInput.model_validate({'request_id':'real-meeting',
            'text':'整理会议纪要和行动项JSON，引用字符区间须逐字匹配正本；明确本人截止时间保留，未定截止保持null。只提出候选，由我稍后确认。',
            'input_files':[{'item_id':source['id'],'revision':1}]}))
        await asyncio.wait_for(p.task_registry.join(),240)
        task=await p.work_tasks.store.get(h.who,task.task_id)
        assert task.status=='waiting_user' and task.outputs_verified and len(task.artifacts)==2,task
        candidates=[e['candidate'] for e in task.action_candidates]
        for candidate in candidates:
            quote=candidate['source'];assert text[quote['start']:quote['end']]==quote['quote']
        own=[c for c in candidates if c['owner']=='self']
        assert any(c.get('due_at')==datetime.fromisoformat(str(due)+'T17:00:00+08:00').timestamp() and '需求' in c['title'] for c in own)
        assert any(c.get('due_at') is None and '归档' in c['title'] for c in own)
        assert not await p.workspace.list(h.who,kind='todo') and not await p.workspace.list(h.who,kind='event')
        hashes={}
        for artifact in task.artifacts:
            content=(await p.workspace.download(h.who,artifact.item_id))[0]
            assert hashlib.sha256(content).hexdigest()==artifact.sha256;hashes[artifact.role]=artifact.sha256
        assessment.update(result='passed',lifecycle=task.status,candidates=candidates,download_sha256=hashes,
            calls=len(models.calls),due_date=str(due),source_sha256=hashlib.sha256(text.encode()).hexdigest())
    finally:save_evidence('meeting',models,assessment)


async def test_real_no_attachment_http_task_has_real_read_receipt(http_world):  # noqa: F811
    """原生批准不调用额外路由模型；真实HTTP查询计数与业务事实分别验证。"""
    w=http_world;p=w.platform;models=real_factory(p);p.runtimes._models=models
    assessment={'result':'failed'}
    try:
        task=await p.work_tasks.accept_capability(w.employee,CapabilityTaskInput.model_validate({
            'request_id':'real-no-attachment','text':'请调用已登记的客户查询接口，读取合成客户并汇报客户编号和状态；不要写文件，不要修改业务。',
            'capability':{'package':'http-recovery','entry':'query'}}))
        for _ in range(4):
            await asyncio.wait_for(p.task_registry.join(),180)
            task=await p.work_tasks.store.get(w.employee,task.task_id)
            if task.status!='waiting_user':break
            assert task.pending and all(e['detail']['target']=='api__counter__read' for e in task.pending)
            decision=ToolApprovalDecision.model_validate({'kind':'tool_approval','request_id':'approve-'+str(task.state_version),
                'state_version':task.state_version,'decisions':{e['request_id']:{'granted':True} for e in task.pending}})
            await p.work_tasks.decide(w.employee,task.task_id,decision)
        assert task.status=='succeeded' and not task.artifacts,task
        assert 'C-R2' in task.result_summary and 'verified' in task.result_summary
        assert len(w.counts)==1 and task.result_receipt.get('tool_receipts')
        assessment.update(result='passed',lifecycle=task.status,summary=task.result_summary,
            read_count=len(w.counts),external_writes=0,calls=len(models.calls))
    finally:save_evidence('no-attachment',models,assessment)

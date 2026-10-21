"""Linux真实Docker和PG在执行/半上传时SIGKILL后继续。"""
from __future__ import annotations

import asyncio
import json

import pytest

TABLE_STEPS = [
    {'calls':[['dispatch','call_skill',{'skill_id':'table-summary','reason':'真实脚本汇总'}]]},
    {'calls':[['script','run_script',{'skill_id':'table-summary','script_name':'summarize','args':{
        'inputs':'["input_1.csv"]','group_column':'部门','amount_column':'金额','currency_column':''}}]]},
    {'text':'子skill文件生成'}, {'text':'根轮核验保存'}]


@pytest.mark.parametrize('identity_stores', ['postgres'], indirect=True)
@pytest.mark.parametrize('phase', ['executing', 'artifact_uploaded:2'])
@pytest.mark.parametrize('stop_mode',['sigkill','sigterm'])
async def test_process_stop_recovers_actual_phase_without_duplicate_artifacts(recovery_world, step, phase,stop_mode):
    """执行前重算用新session；已冻结补传不再调用模型。"""
    harness = recovery_world
    await harness.accept_office()
    harness.steps = [{'text':'等待中','hold':True}] if phase == 'executing' else TABLE_STEPS
    before = await harness.run_until(phase)
    if stop_mode == 'sigkill':
        await harness.crash()
    else:
        await harness.stop()
        assert (await harness.read_dispatch()).state == 'paused_shutdown'
        assert (await harness.read_task()).status == 'running'
    if phase == 'executing':
        harness.world.models.script(*(step(**value) for value in TABLE_STEPS))
    await harness.reopen(advance_seconds=31 if phase == 'executing' and stop_mode == 'sigkill' else 0)
    current = await harness.drain()
    assert current.status == 'succeeded' and not current.resources_pending, current
    assert current.attempt_no == 2 and len(current.artifacts) == 4
    assert [entry.model_dump() for entry in current.inputs] == before['inputs']
    if phase == 'executing':
        assert current.session_id != before['session_id']
        assert harness.model_calls > before['model_calls']
    else:
        assert harness.model_calls == before['model_calls']
    assert (await harness.counts())['artifact_files'] == 4
    if before['container']:
        process = await asyncio.create_subprocess_exec('docker','inspect','--format','{{.State.Running}}',
            before['container'],stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        output,_ = await process.communicate()
        assert process.returncode != 0 or output.strip() == b'false'
    evidence = next(entry for entry in current.artifacts if entry.role == 'evidence')
    data = (await harness.platform.workspace.download(harness.who, evidence.item_id))[0]
    assert json.loads(data)['totals'][0]['amount'] == '15.50'


@pytest.mark.parametrize('identity_stores', ['postgres'], indirect=True)
async def test_origin_key_revoked_after_pinned_inputs_prevents_recovery(recovery_world):
    """真实key吊销与SIGKILL组合，下一owner零模型调用且已知资源退出。"""
    h = recovery_world
    await h.accept_office(origin='act_as_key')
    h.steps = [{'text':'尚未调用模型','hold':True}]
    before = await h.run_until('inputs_pinned')
    assert before['model_calls'] == 0
    await h.revoke_origin_key()
    await h.crash()
    await h.reopen()
    dispatch = await h.read_dispatch()
    task = await h.read_task()
    assert dispatch.state == 'blocked' and dispatch.block_reason == 'grant_revoked'
    assert not task.resources_pending and h.model_calls == 0

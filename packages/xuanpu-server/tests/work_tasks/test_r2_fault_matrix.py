"""R2-C跨层故障矩阵：真实PG、SIGKILL、Linux成果与通知联合核对。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from xuanpu_server.work_tasks.activity import ActivityConfig

from .test_phase_recovery import TABLE_STEPS


@pytest.mark.parametrize('identity_stores',['postgres'],indirect=True)
@pytest.mark.parametrize('phase',['accepted','artifacts_staged','artifact_uploaded:2'])
async def test_crash_matrix_preserves_one_task_four_hashes_and_one_notification(recovery_world,step,phase):
    """只在真实提交后强杀，原始事件和下载摘要跨重建保持可核对。"""
    h=recovery_world
    h.platform.store.workspace_store.configure_activity(ActivityConfig(enabled=True))
    task=await h.accept_office()
    before_events=await h.platform.work_tasks.store.events(h.who,task.task_id)
    h.steps=TABLE_STEPS
    before=await h.run_until(phase)
    await h.crash()
    if phase=='accepted':h.world.models.script(*(step(**value) for value in TABLE_STEPS))
    await h.reopen()
    final=await h.drain()
    assert final.status=='succeeded' and len(final.artifacts)==4 and not final.resources_pending
    assert len(await h.platform.work_tasks.store.list(h.who))==1
    assert final.task_id==task.task_id and [i.model_dump() for i in final.inputs]==before['inputs']
    if phase!='accepted':assert h.model_calls==before['model_calls']
    hashes={}
    for artifact in final.artifacts:
        content=(await h.platform.workspace.download(h.who,artifact.item_id,artifact.revision))[0]
        digest=hashlib.sha256(content).hexdigest()
        assert digest==artifact.sha256
        hashes[artifact.role]=digest
    events=await h.platform.work_tasks.store.events(h.who,task.task_id)
    assert events[0].event_id==before_events[0].event_id
    assert len({e.event_id for e in events})==len(events)
    assert [e.state_version for e in events]==sorted({e.state_version for e in events})
    notifications=await h.notifications(task.task_id)
    assert len(notifications)==1 and notifications[0].title=='工作已完成'
    await h.reconcile_again()
    assert await h.notifications(task.task_id)==notifications
    output=os.environ.get('XUANPU_R2_EVIDENCE_DIR')
    if output:
        directory=Path(output)/('fault-'+phase.replace(':','-'));directory.mkdir(parents=True,exist_ok=False)
        (directory/'assessment.json').write_text(json.dumps({'phase':phase,'signal':'SIGKILL','database':'PostgreSQL',
            'executor':'Linux Docker','task_id':task.task_id,'attempt':final.attempt_no,'hashes':hashes,
            'model_calls':h.model_calls,'notifications':len(notifications),'artifact_count':4,
            'event_ids':[e.event_id for e in events]},ensure_ascii=False,indent=2))


@pytest.mark.parametrize('identity_stores',['postgres'],indirect=True)
async def test_sigkill_while_actual_office_python_script_runs(recovery_world,step):
    """脚本在真实Linux沙盒写入运行标记后才强杀平台，恢复先退出旧容器。"""
    import asyncio
    import shutil

    import yaml
    from xuanpu_spec import pack_package
    h=recovery_world
    package=h.directory/'slow-office'
    shutil.copytree(Path(__file__).resolve().parents[4]/'examples/office-assistant',package)
    manifest=yaml.safe_load((package/'xuanpu.yaml').read_text());manifest['metadata']['version']='1.0.7'
    (package/'xuanpu.yaml').write_text(yaml.safe_dump(manifest,allow_unicode=True))
    skill=package/'skills/table-summary/SKILL.md'
    text=skill.read_text().replace('scripts:\n','scripts:\n  - name: hold\n    path: scripts/hold.py\n    language: python\n    timeout_seconds: 300\n    max_output_bytes: 1024\n    args_schema: {type: object, properties: {}}\n',1)
    skill.write_text(text)
    (package/'skills/table-summary/scripts/hold.py').write_text("import os,time\nfrom pathlib import Path\np=Path(os.environ['WORKSPACE'])/'scratch/hold-ready'\np.parent.mkdir(exist_ok=True)\np.write_text('running')\ntime.sleep(300)\n")
    await h.platform.packages.publish(h.world.admin,pack_package(package),activate=True)
    await h.accept_office()
    h.steps=[{'calls':[['child','call_skill',{'skill_id':'table-summary','reason':'合成脚本故障验收'}]]},
        {'calls':[['hold','run_script',{'skill_id':'table-summary','script_name':'hold','args':{}}]]}]
    before=await h.run_until('script_running')
    assert before['container'] and before['model_calls']>=2
    await h.crash()
    h.world.models.script(*(step(**value) for value in TABLE_STEPS))
    await h.reopen(advance_seconds=31)
    final=await h.drain()
    assert final.status=='succeeded' and final.attempt_no==2 and len(final.artifacts)==4
    assert final.session_id!=before['session_id']
    process=await asyncio.create_subprocess_exec('docker','inspect','--format','{{.State.Running}}',
        before['container'],stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    output,_=await process.communicate()
    assert process.returncode!=0 or output.strip()==b'false'

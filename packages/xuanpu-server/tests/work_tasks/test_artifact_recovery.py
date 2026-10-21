"""保存中断只能恢复已有文件索引，不能重放模型、脚本或新增上传。"""

from __future__ import annotations

import json

import pytest
from xuanpu_server.runtime.sandbox import SandboxHandle
from xuanpu_server.work_tasks.artifacts import ArtifactService
from xuanpu_server.work_tasks.store import WorkTaskStore


@pytest.mark.parametrize('fail_stage', ['second_upload', 'index_commit'])
async def test_recovery_indexes_only_completed_uploads(task_env, tmp_path, monkeypatch, fail_stage):
    """实际对象写入故障或索引事务失败后，使用已有幂等回执恢复准确子集。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    task, _ = await store.create_or_replay(env.who, env.record)
    task = await store.transition(env.who, task.task_id, 'running', expected_version=1, at=1001.0)
    outputs = tmp_path / 'outputs';outputs.mkdir()
    (outputs/'minutes.md').write_text('# 纪要\nprivate minutes')
    (outputs/'actions.json').write_text('[]')
    (outputs/'manifest.json').write_text(json.dumps({'kind':'meeting_minutes','artifacts':[
        {'path':'outputs/minutes.md','name':'纪要.md','role':'minutes','mime_type':'text/markdown'},
        {'path':'outputs/actions.json','name':'行动项.json','role':'actions','mime_type':'application/json'}]}))

    async def freeze():
        """文件已停止生产，真实容器冻结另由Docker用例覆盖。"""

    handle = SandboxHandle(executor=None, workspace=tmp_path, workspace_inside='/workspace',
                           backend='docker', freezer=freeze)
    service = ArtifactService(env.workspace, store, now=lambda:1002.0)
    original_put, original_transition = env.workspace.objects.put, store.transition
    puts = []

    async def failing_put(*args, **kwargs):
        """只在对象存储边界注入故障，事务及第一份文件均是真实写入。"""
        puts.append(args[0])
        if fail_stage == 'second_upload' and len(puts) == 2:
            raise OSError('受控对象存储故障')
        return await original_put(*args, **kwargs)

    async def failing_transition(*args, **kwargs):
        """模拟所有上传已完成但最终索引提交前进程退出。"""
        if fail_stage == 'index_commit' and kwargs.get('event_kind') == 'artifacts_saved':
            raise OSError('受控索引提交故障')
        return await original_transition(*args, **kwargs)

    monkeypatch.setattr(env.workspace.objects, 'put', failing_put)
    monkeypatch.setattr(store, 'transition', failing_transition)
    with pytest.raises(OSError):
        await service.commit(env.who, task, handle)
    current = await store.get(env.who, task.task_id)
    assert current.artifact_recovery_pending and not current.artifacts
    await store.settle_lifecycle('acme','usr_a',task.task_id,'interrupted',at=1003.0,error_code='process_restarted')
    await service.recover_index(current)
    await service.recover_index(current)
    recovered = await store.get(env.who, task.task_id)
    count = 1 if fail_stage == 'second_upload' else 2
    assert len(recovered.artifacts) == count and not recovered.artifact_recovery_pending
    assert recovered.status == 'interrupted' and recovered.outputs_verified == (count == 2)
    assert len(puts) == 2
    assert len(await env.workspace.list(env.who,kind='file')) == count + 1
    for entry in recovered.artifacts:
        content, _, _ = await env.workspace.download(env.who,entry.item_id,entry.revision)
        assert content

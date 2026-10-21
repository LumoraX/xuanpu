"""真实对象存储中断补传与篡改检查。"""
from __future__ import annotations

import hashlib

import pytest
from xuanpu_server.errors import ConflictError
from xuanpu_server.work_tasks.artifact_staging import ArtifactStaging
from xuanpu_server.work_tasks.execution_context import ExecutionBinding, current_execution
from xuanpu_server.work_tasks.models import ArtifactIntent

from .test_execution_store import prepared


@pytest.mark.parametrize('tamper', [False, True])
async def test_staged_bytes_restore_missing_upload_without_execution(task_env, monkeypatch, tamper):
    """中途失败后只从私有字节继续，已有上传回执不产生第二份文件。"""
    env = task_env
    tasks, execution, task = await prepared(env)
    lease = await execution.claim(env.who, task.task_id, owner='p', now=1000.)
    task = await tasks.get(env.who, task.task_id)
    task = await tasks.transition(env.who, task.task_id, 'running', expected_version=task.state_version, at=1000.)
    staging = ArtifactStaging(env.workspace, tasks, lambda:1000.)
    token = current_execution.set(ExecutionBinding(lease, execution, lambda:1000.))
    data = [b'# minutes', b'[]']
    intents = tuple(ArtifactIntent(name=f'{i}.md', role='minutes', mime_type='text/markdown',
        size=len(content), sha256=hashlib.sha256(content).hexdigest()) for i, content in enumerate(data))
    try:
        await staging.stage(env.who, task, intents, data)
        original = env.workspace.upload
        async def failing(*args, **kwargs):
            if args[4].endswith(':1'):
                raise OSError('第二份上传中断')
            return await original(*args, **kwargs)
        monkeypatch.setattr(env.workspace, 'upload', failing)
        with pytest.raises(OSError):
            await staging.commit(env.who, task)
        monkeypatch.setattr(env.workspace, 'upload', original)
        frozen = await tasks.get(env.who, task.task_id)
        if tamper:
            await env.workspace.objects.put(frozen.frozen_staging[1], b'XX')
            with pytest.raises(ConflictError):
                await staging.commit(env.who, frozen)
        else:
            receipts = await staging.commit(env.who, frozen)
            again = await staging.commit(env.who, frozen)
            assert [r.item_id for r in receipts] == [r.item_id for r in again]
            assert len(await env.workspace.list(env.who, kind='file')) == 3
            for receipt, content in zip(receipts, data, strict=True):
                assert (await env.workspace.download(env.who, receipt.item_id))[0] == content
    finally:
        current_execution.reset(token)

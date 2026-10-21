"""成果必须经过真实冻结、格式校验和幂等保存，清单不是成功回执。"""

from __future__ import annotations

import json

import pytest
from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.runtime.sandbox import SandboxHandle
from xuanpu_server.work_tasks.artifacts import ArtifactService
from xuanpu_server.work_tasks.store import WorkTaskStore


async def test_artifacts_are_verified_and_saved_once(task_env, tmp_path):
    """冻结在读取前，重复导入同一成果只产生一份个人文件。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    task, _ = await store.create_or_replay(env.who, env.record)
    task = await store.transition(env.who, task.task_id, 'running', expected_version=1, at=1001.0)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    (outputs / 'minutes.md').write_text('# 会议纪要\nprivate minutes')
    (outputs / 'actions.json').write_text('[]')
    manifest = {'kind': 'meeting_minutes', 'artifacts': [
        {'path': 'outputs/minutes.md', 'name': '会议纪要.md', 'role': 'minutes', 'mime_type': 'text/markdown'},
        {'path': 'outputs/actions.json', 'name': '行动项.json', 'role': 'actions', 'mime_type': 'application/json'}]}
    (outputs / 'manifest.json').write_text(json.dumps(manifest))
    frozen = []

    async def freeze():
        """记录冻结操作；实际Docker冻结另有后端测试。"""
        frozen.append(True)

    handle = SandboxHandle(executor=None, workspace=tmp_path, workspace_inside='/workspace',
                           backend='docker', freezer=freeze)
    service = ArtifactService(env.workspace, store, now=lambda: 1002.0)
    receipts, actions = await service.commit(env.who, task, handle)
    again, _ = await service.commit(env.who, task, handle)
    assert receipts == again and actions == []
    assert len(frozen) == 1
    assert len(await env.workspace.list(env.who, kind='file')) == 3
    assert all(receipt.input_refs[0].revision == 1 for receipt in receipts)
    assert (await store.get(env.who, task.task_id)).outputs_verified
    content, _, _ = await env.workspace.download(env.who, receipts[0].item_id)
    assert b'private minutes' in content


@pytest.mark.parametrize('path', ['../outside.txt', '/outside.txt', 'outputs/empty.md'])
async def test_invalid_manifest_cannot_become_success(task_env, tmp_path, path):
    """非法候选在任何上传之前失败，任务不会被标为成功。"""
    env = task_env
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    (outputs / 'empty.md').touch()
    (outputs / 'manifest.json').write_text(json.dumps({'kind': 'meeting_minutes', 'artifacts': [
        {'path': path, 'name': '纪要.md', 'role': 'minutes', 'mime_type': 'text/markdown'}]}))

    async def freeze():
        """模拟明确支持冻结的后端。"""

    handle = SandboxHandle(executor=None, workspace=tmp_path, workspace_inside='/workspace',
                           backend='docker', freezer=freeze)
    with pytest.raises(InvalidRequestError):
        await ArtifactService(env.workspace, WorkTaskStore(env.workspace)).commit(env.who, env.record, handle)
    assert len(await env.workspace.list(env.who, kind='file')) == 1

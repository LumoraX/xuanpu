"""固定输入只读取受理时的本人版本，并验证实际字节与内容格式。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.work_tasks.inputs import InputSnapshotService


async def test_stage_pins_old_revision_in_separate_root(task_env, tmp_path):
    """更新文件后，任务仍读取受理版本，名称来自服务器。"""
    env = task_env
    await env.workspace.replace_content(env.who, env.file['id'], b'new material', 'text/plain',
                                        env.file['version'], 'replace')
    root = await InputSnapshotService(env.workspace, tmp_path / 'snapshots').stage(env.who, env.record)
    assert (root / 'input_1.txt').read_bytes() == b'private minutes'
    assert {path.name for path in root.iterdir()} == {'input_1.txt'}
    assert root.parent.parent == tmp_path / 'snapshots'


async def test_stage_revalidates_owner_recycle_and_checksum(task_env, tmp_path):
    """禁止他人、已回收来源以及与固定摘要不符的内容进入沙盒。"""
    env = task_env
    service = InputSnapshotService(env.workspace, tmp_path / 'snapshots')
    with pytest.raises(NotFoundError):
        await service.stage(replace(env.who, user_id='usr_b'), env.record)
    bad = env.record.model_copy(update={'inputs': (env.record.inputs[0].model_copy(update={'sha256': 'f'*64}),)})
    with pytest.raises(ConflictError):
        await service.stage(env.who, bad)
    await env.workspace.trash(env.who, env.file['id'], env.file['version'])
    with pytest.raises((NotFoundError, ConflictError)):
        await service.stage(env.who, env.record)
    assert not (tmp_path / 'snapshots').exists()


@pytest.mark.parametrize('mime,content', [('text/plain', b'\x00bad'), ('application/pdf', b'not pdf'),
    ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', b'PKfake')])
def test_content_cannot_masquerade_by_mime(mime, content):
    """元数据和后缀不是格式验证。"""
    from xuanpu_server.work_tasks.formats import validate_content

    with pytest.raises(InvalidRequestError):
        validate_content(content, mime)

"""F13实际文件系统边界、不可变性与真实材料worker责任链。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.objects import EncryptedLocalObjects

from .f13_objects_helpers import CONTENT, KEY, fifo_probe, snapshot
from .test_storage import WHO, prepare
from .test_worker import Gateway, ref

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


@pytest.mark.parametrize('level', ['root', 'shard', 'leaf'])
@pytest.mark.parametrize('action', ['get', 'put'])
async def test_symlink_rejects_valid_external_cipher(
    tmp_path: Path, level: str, action: str, record_property: Any
) -> None:
    """相同AAD合法密文不能使root、shard、leaf链接越界读或写。"""
    root, outside = tmp_path/'objects', tmp_path/'outside'
    outside.mkdir()
    objects = EncryptedLocalObjects(root, b'k'*32)
    path = objects._path(KEY)
    encrypted = objects.envelope.encrypt(KEY, CONTENT)
    if level == 'root':
        (outside/path.parent.name).mkdir()
        target = outside/path.parent.name/path.name
        root.symlink_to(outside, target_is_directory=True)
    elif level == 'shard':
        root.mkdir()
        path.parent.symlink_to(outside, target_is_directory=True)
        target = outside/path.name
    else:
        path.parent.mkdir(parents=True)
        target = outside/'cipher.enc'
        path.symlink_to(target)
    target.write_bytes(encrypted)
    before = snapshot(outside)
    try:
        with pytest.raises(FinanceError, match='OBJECT_PATH_UNSAFE') as error:
            if action == 'get':
                await objects.get(KEY)
            else:
                await objects.put(KEY, CONTENT)
        assert error.value.status == 500
        assert str(root) not in error.value.message
    finally:
        record_property('f13_objects', json.dumps({'case': level+'_'+action,
                        'outside_before': before, 'outside_after': snapshot(outside),
                        'external_snapshot_equal': before == snapshot(outside)}))
        assert snapshot(outside) == before


@pytest.mark.parametrize('level', ['root', 'shard'])
async def test_symlink_cannot_create_external_leaf(
    tmp_path: Path, level: str, record_property: Any
) -> None:
    """外部叶子尚不存在时也不能经链接创建新密文。"""
    root, outside = tmp_path/'objects', tmp_path/'outside'
    outside.mkdir()
    objects = EncryptedLocalObjects(root, b'k'*32)
    path = objects._path(KEY)
    if level == 'root':
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        path.parent.symlink_to(outside, target_is_directory=True)
    before = snapshot(outside)
    try:
        with pytest.raises(FinanceError, match='OBJECT_PATH_UNSAFE'):
            await objects.put(KEY, CONTENT)
    finally:
        record_property('f13_objects', json.dumps({'case': 'new_'+level,
                        'external_snapshot_equal': snapshot(outside) == before}))
        assert snapshot(outside) == before


@pytest.mark.parametrize('kind', ['file', 'dangling', 'link_to_file'])
@pytest.mark.parametrize('action', ['get', 'put'])
async def test_unsafe_root_has_stable_error(tmp_path: Path, kind: str, action: str) -> None:
    """普通文件和两类非法根链接返回固定领域错误。"""
    root = tmp_path/'objects'
    if kind == 'file':
        root.write_bytes(b'root file')
    else:
        target = tmp_path/'target'
        if kind == 'link_to_file':
            target.write_bytes(b'target file')
        root.symlink_to(target)
    objects = EncryptedLocalObjects(root, b'k'*32)
    with pytest.raises(FinanceError, match='OBJECT_PATH_UNSAFE') as error:
        if action == 'get':
            await objects.get(KEY)
        else:
            await objects.put(KEY, CONTENT)
    assert error.value.status == 500


@pytest.mark.parametrize('kind', ['fifo', 'socket'])
@pytest.mark.parametrize('action', ['get', 'put'])
def test_special_leaf_is_rejected_without_blocking(tmp_path: Path, kind: str, action: str) -> None:
    """FIFO由可杀死子进程验证，socket也必须静态拒绝。"""
    objects = EncryptedLocalObjects(tmp_path/'objects', b'k'*32)
    path = objects._path(KEY)
    path.parent.mkdir(parents=True)
    if kind == 'fifo':
        os.mkfifo(path)
        result = fifo_probe(objects.root, action)
    else:
        previous = os.getcwd()
        with socket.socket(socket.AF_UNIX) as stream:
            try:
                os.chdir(path.parent)
                stream.bind(path.name)
            finally:
                os.chdir(previous)
            result = fifo_probe(objects.root, action)
    assert result == {'code': 'OBJECT_PATH_UNSAFE', 'status': 500}


async def test_missing_and_existing_immutable_controls(tmp_path: Path) -> None:
    """正常缺件保持原404且不建根，同内容幂等与不同内容冲突保持原契约。"""
    objects = EncryptedLocalObjects(tmp_path/'objects', b'k'*32)
    with pytest.raises(FinanceError, match='OBJECT_NOT_FOUND'):
        await objects.get(KEY)
    assert not objects.root.exists()
    await objects.put(KEY, CONTENT)
    before = objects._path(KEY).read_bytes()
    await EncryptedLocalObjects(objects.root, b'k'*32).put(KEY, CONTENT)
    with pytest.raises(FinanceError, match='OBJECT_IMMUTABLE'):
        await objects.put(KEY, b'different')
    assert objects._path(KEY).read_bytes() == before
    assert await objects.get(KEY) == CONTENT


@pytest.mark.parametrize('same', [False, True])
async def test_two_adapters_race_preserves_one_winner(tmp_path: Path, same: bool) -> None:
    """两个适配器真实并发创建，相同内容均成功，不同内容只有一个胜者。"""
    adapters = [EncryptedLocalObjects(tmp_path, b'k'*32) for _ in range(2)]
    payloads = [CONTENT, CONTENT if same else b'competing original']
    results = await asyncio.gather(*(item.put(KEY, body) for item, body in zip(adapters, payloads)),
                                   return_exceptions=True)
    assert sum(result is None for result in results) == (2 if same else 1)
    failures = [result for result in results if result is not None]
    assert all(isinstance(result, FinanceError) and result.code == 'OBJECT_IMMUTABLE'
               for result in failures)
    winner = next(body for body, result in zip(payloads, results) if result is None)
    assert await adapters[0].get(KEY) == winner
    assert len(list(tmp_path.rglob('*'))) == 2


@pytest.mark.parametrize('digest', ['A'*64, 'g'*64, 'a'*63, 'a'*65, '../escape', ''])
def test_local_files_require_exact_digest(tmp_path: Path, digest: str) -> None:
    """纯文件边界只允许64位小写十六进制摘要。"""
    from xuanpu_finance.local_objects import LocalObjectFiles

    files = LocalObjectFiles(tmp_path/'absent')
    for call in [lambda: files.read(digest), lambda: files.write_once(digest, b'cipher')]:
        with pytest.raises(ValueError, match='对象摘要无效'):
            call()
    assert not files.root.exists()


def test_files_private_modes_and_existing_cipher(tmp_path: Path) -> None:
    """新目录与叶子保持私有权限，重复创建返回既有密文且无临时文件。"""
    from xuanpu_finance.local_objects import LocalObjectFiles

    root = tmp_path/'objects'
    files = LocalObjectFiles(root)
    digest = hashlib.sha256(KEY.encode()).hexdigest()
    assert files.write_once(digest, b'first cipher') is None
    assert files.write_once(digest, b'other cipher') == b'first cipher'
    path = root/digest[:2]/(digest+'.enc')
    assert root.stat().st_mode & 0o777 == 0o700
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert files.read(digest) == b'first cipher'
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('action', ['read', 'write'])
def test_io_fault_propagates_and_all_owned_fds_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """真实文件打开或链接故障原样传播，已经创建的临时文件和所有描述符回收。"""
    import errno

    from xuanpu_finance import local_objects

    files = local_objects.LocalObjectFiles(tmp_path)
    digest = hashlib.sha256(KEY.encode()).hexdigest()
    files.write_once(digest, b'cipher')
    actual_open = os.open
    descriptors: list[int] = []
    failure = OSError(errno.EIO, 'injected IO failure')

    def observed(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        """保留真实打开操作，在叶子读取边界注入实际IO错误。"""
        if action == 'read' and path == digest+'.enc':
            raise failure
        descriptor = actual_open(path, flags, mode, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def broken_link(*args: Any, **kwargs: Any) -> None:
        """模拟文件系统链接失败而不创建最终叶子。"""
        raise failure

    monkeypatch.setattr(local_objects.os, 'open', observed)
    if action == 'write':
        monkeypatch.setattr(local_objects.os, 'link', broken_link)
    with pytest.raises(OSError) as error:
        if action == 'read':
            files.read(digest)
        else:
            files.write_once(digest, b'new cipher')
    assert error.value is failure
    for descriptor in descriptors:
        with pytest.raises(OSError, match='Bad file descriptor'):
            os.fstat(descriptor)
    assert [path.name for path in (tmp_path/digest[:2]).iterdir()] == [digest+'.enc']


def test_temp_create_failure_never_deletes_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """私有随机临时名发生碰撞时，不能删除其他写入者的同名文件。"""
    from xuanpu_finance import local_objects

    digest = hashlib.sha256(KEY.encode()).hexdigest()
    shard = tmp_path/digest[:2]
    shard.mkdir()
    victim = shard/('.object-'+'a'*32+'.tmp')
    victim.write_bytes(b'existing writer temporary')
    monkeypatch.setattr(local_objects.secrets, 'token_hex', lambda size: 'a'*32)
    with pytest.raises(FileExistsError):
        local_objects.LocalObjectFiles(tmp_path).write_once(digest, b'cipher')
    assert victim.read_bytes() == b'existing writer temporary'
    assert list(shard.iterdir()) == [victim]


@pytest.mark.parametrize('level', ['root', 'shard'])
@pytest.mark.parametrize('action', ['get', 'put'])
async def test_directory_swap_before_open_never_follows_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str, action: str,
    record_property: Any
) -> None:
    """实际open之前换成新链接，拒绝跟随其外部目标。"""
    root, outside = tmp_path/'objects', tmp_path/'outside'
    objects = EncryptedLocalObjects(root, b'k'*32)
    await objects.put(KEY, CONTENT)
    target = objects._path(KEY)
    outside.mkdir()
    external_shard = outside/target.parent.name
    external_shard.mkdir()
    (external_shard/target.name).write_bytes(objects.envelope.encrypt(KEY, CONTENT))
    (outside/target.name).write_bytes(objects.envelope.encrypt(KEY, CONTENT))
    before = snapshot(outside)
    original_open = os.open
    changed = False

    def swapped(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        """在对应目录真正打开前执行一次实际rename和symlink。"""
        nonlocal changed
        watched = root if level == 'root' else target.parent.name
        if not changed and path == watched:
            changed = True
            directory = root if level == 'root' else target.parent
            directory.rename(tmp_path/'moved')
            directory.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, 'open', swapped)
    with pytest.raises(FinanceError, match='OBJECT_PATH_UNSAFE'):
        if action == 'get':
            await objects.get(KEY)
        else:
            await objects.put(KEY, CONTENT)
    assert changed and snapshot(outside) == before
    record_property('f13_objects', json.dumps({'case': 'open_race_'+level+'_'+action,
                    'external_snapshot_equal': before == snapshot(outside)}))


@pytest.mark.parametrize('level', ['root', 'shard'])
async def test_directory_swap_at_link_uses_opened_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str, record_property: Any
) -> None:
    """打开目录被移走后允许向旧fd成功写，不能跟随当前名字的新链接。"""
    root, outside = tmp_path/'objects', tmp_path/'outside'
    objects = EncryptedLocalObjects(root, b'k'*32)
    path = objects._path(KEY)
    outside.mkdir()
    before = snapshot(outside)
    actual_link = os.link
    changed = False

    def swapped(source: Any, destination: Any, **kwargs: Any) -> None:
        """真正link调用前移动目录并放入外部链接。"""
        nonlocal changed
        changed = True
        directory = root if level == 'root' else path.parent
        directory.rename(tmp_path/'moved')
        directory.symlink_to(outside, target_is_directory=True)
        actual_link(source, destination, **kwargs)

    monkeypatch.setattr(os, 'link', swapped)
    await objects.put(KEY, CONTENT)
    assert changed and snapshot(outside) == before
    moved = tmp_path/'moved'
    stored = moved/path.parent.name/path.name if level == 'root' else moved/path.name
    assert objects.envelope.decrypt(KEY, stored.read_bytes()) == CONTENT
    assert len(list(stored.parent.iterdir())) == 1
    record_property('f13_objects', json.dumps({'case': 'link_race_'+level,
                    'external_snapshot_equal': before == snapshot(outside),
                    'opened_directory_write_succeeded': True}))


@pytest.mark.parametrize('replacement', ['fifo', 'symlink'])
def test_leaf_swap_after_stat_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    """stat后叶子被换成FIFO或链接，非阻塞open和fstat二次检查仍拒绝。"""
    from xuanpu_finance.local_objects import LocalObjectFiles

    files = LocalObjectFiles(tmp_path)
    digest = hashlib.sha256(KEY.encode()).hexdigest()
    files.write_once(digest, b'cipher')
    path = tmp_path/digest[:2]/(digest+'.enc')
    original_open = os.open

    def swapped(name: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        """保留实际open，在叶子边界置换文件。"""
        if name == digest+'.enc':
            path.unlink()
            if replacement == 'fifo':
                os.mkfifo(path)
            else:
                path.symlink_to(tmp_path/'outside')
        return original_open(name, flags, mode, **kwargs)

    monkeypatch.setattr(os, 'open', swapped)
    with pytest.raises(FinanceError, match='OBJECT_PATH_UNSAFE'):
        files.read(digest)


async def test_material_worker_preserves_failed_responsibility_then_processes_next(
    store: FinanceStore, tmp_path: Path, record_property: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实材料坏shard失败保留prepared无回执，同一worker随后处理安全不同shard。"""
    from xuanpu_finance.storage import imports

    # 只固定受理操作及trace的ID，真实产品仍生成责任键并执行完整持久化和worker。
    identifiers = iter(f'{index:032x}' for index in range(1, 5))
    monkeypatch.setattr(imports, 'identifier', lambda: next(identifiers))
    await prepare(store)
    root, outside = tmp_path/'objects', tmp_path/'outside'
    root.mkdir()
    outside.mkdir()
    (outside/'sentinel').write_bytes(b'external unchanged')
    before = snapshot(outside)
    operation = await store.accept_supporting_materials(WHO, 'company-a', [ref('bad', CONTENT)],
                                                       'f13-bad-shard')
    objects = EncryptedLocalObjects(root, b'k'*32)
    key = f'{WHO.tenant_id}/company-a/{operation["id"]}/0/source'
    objects._path(key).parent.symlink_to(outside, target_is_directory=True)
    worker = FinanceWorker(store, objects, Gateway({'bad': CONTENT, 'good': CONTENT}))
    assert await worker.run_once()
    failed = await store.get_operation(WHO, operation['id'])
    item = await store.pool.fetchrow('SELECT * FROM finance_item WHERE operation_id=$1', operation['id'])
    events = await store.pool.fetch('SELECT kind FROM finance_event WHERE aggregate_id=$1', operation['id'])
    kinds = [event['kind'] for event in events]
    receipt_count = await store.pool.fetchval('SELECT count(*) FROM finance_receipt')
    evidence = {'outside_before': before, 'outside_after': snapshot(outside),
                'external_snapshot_equal': snapshot(outside) == before,
                'failed_state': failed['items'][0]['state'],
                'failed_error': failed['items'][0].get('error_code'),
                'prepared_count': kinds.count('original.prepared'),
                'receipt_count_after_failed': receipt_count,
                'material_success_count_after_failed': kinds.count('material.received')}
    record_property('f13_objects', json.dumps(evidence))
    assert failed['status'] == 'failed' and failed['items'][0]['state'] == 'failed'
    assert failed['items'][0]['error_code'] == 'OBJECT_STORAGE_FAILED'
    assert item is not None and item['object_key'] == key and item['receipt_id'] is None
    assert item['actual_sha256'] == hashlib.sha256(CONTENT).hexdigest()
    assert kinds.count('original.prepared') == 1
    assert receipt_count == 0 and kinds.count('material.received') == 0
    assert snapshot(outside) == before
    next_operation = await store.accept_supporting_materials(
        WHO, 'company-a', [ref('good', CONTENT)], 'f13-next-safe')
    next_key = f'{WHO.tenant_id}/company-a/{next_operation["id"]}/0/source'
    assert objects._path(next_key).parent != objects._path(key).parent
    assert await worker.run_once()
    succeeded = await store.get_operation(WHO, next_operation['id'])
    assert succeeded['status'] == 'succeeded' and succeeded['summary']['materials'] == 1
    assert await objects.get(next_key) == CONTENT
    next_receipts = await store.pool.fetchval('SELECT count(*) FROM finance_receipt')
    material_events = await store.pool.fetchval(
        "SELECT count(*) FROM finance_event WHERE kind='material.received'")
    assert next_receipts == 1 and material_events == 1
    assert snapshot(outside) == before
    record_property('f13_objects', json.dumps({'next_state': succeeded['items'][0]['state'],
                    'next_status': succeeded['status'], 'receipt_count_after_next': next_receipts,
                    'material_success_count_after_next': material_events,
                    'external_snapshot_equal': snapshot(outside) == before,
                    'different_shard': True}))


@pytest.mark.parametrize('action', ['get', 'put'])
async def test_real_permission_error_is_not_relabelled(tmp_path: Path, action: str) -> None:
    """实际撤销叶子或分片权限产生的IO失败必须原样传播。"""
    objects = EncryptedLocalObjects(tmp_path, b'k'*32)
    await objects.put(KEY, CONTENT)
    path = objects._path(KEY)
    changed = path if action == 'get' else path.parent
    changed.chmod(0)
    try:
        with pytest.raises(PermissionError):
            if action == 'get':
                await objects.get(KEY)
            else:
                await objects.put(KEY, CONTENT)
    finally:
        changed.chmod(0o600 if action == 'get' else 0o700)
    assert await objects.get(KEY) == CONTENT
    assert list(path.parent.iterdir()) == [path]


def test_missing_posix_capability_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少目录描述符能力时直接失败，不能退化为路径IO。"""
    from xuanpu_finance import local_objects

    monkeypatch.setattr(local_objects, '_DIR_FD_SUPPORTED', False)
    with pytest.raises(FinanceError, match='OBJECT_PATH_UNSAFE'):
        local_objects.LocalObjectFiles(tmp_path/'absent')
    assert not (tmp_path/'absent').exists()

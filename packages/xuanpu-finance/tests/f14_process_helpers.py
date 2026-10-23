"""F14真实维护拒绝、配套快照和目标库续办的安全证据辅助。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import asyncpg
from xuanpu_finance.maintenance import MaintenanceOptions
from xuanpu_finance.maintenance_files import verify_manifest
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.storage import FinanceStore

from .f10_recovery_helpers import write_config
from .f11_verify_helpers import snapshot
from .f13_process_helpers import receipt_ciphertext
from .test_f4_maintenance import pg_tools

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from .f12_process_helpers import ProcessCase


async def actual_cli(action: str, options: MaintenanceOptions, package: Path,
                     tool: Path) -> dict[str, Any]:
    """执行真实CLI，报告仅保留有限JSON和退出，不回显任何实际凭据。"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'xuanpu_finance.maintenance_cli', action,
        '--database-dsn-file', str(options.database_dsn_file), '--objects', str(options.object_root),
        '--key-file', str(options.key_file), '--package', str(package), '--pg-tool', str(tool),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(process.communicate(), 60)
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.communicate(), 15)
    combined = output + error
    secrets = [options.database_dsn_file.read_bytes(), options.key_file.read_bytes()]
    paths = [options.database_dsn_file, options.key_file, options.object_root, package, tool]
    assert all(value not in combined for value in secrets)
    assert all(str(path).encode() not in combined for path in paths)
    assert b'Traceback' not in combined and error == b''
    assert len(output) < 4096
    return {'exit': process.returncode, 'output': json.loads(output),
            'stderr_empty': error == b'', 'secrets_paths_trace_absent': True}


async def database_snapshot(dsn: str, root: Path) -> tuple[Any, Any]:
    """单次只读连接读取全部public表和密文，完成后立即关闭目标连接。"""
    connection = await asyncpg.connect(dsn)
    try:
        async with connection.transaction(isolation='repeatable_read', readonly=True):
            return await snapshot(SimpleNamespace(pool=connection), root)
    finally:
        await connection.close()


def private_copy(options: MaintenanceOptions, directory: Path, field: str,
                 mode: str) -> MaintenanceOptions:
    """只在本例私有副本制造宽权限或叶子链接，绝不改产品凭据。"""
    directory.mkdir()
    copies = []
    for name, source in [('dsn', options.database_dsn_file), ('key', options.key_file)]:
        target = directory / name
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
        if name == field:
            if mode == '0644':
                target.chmod(0o644)
            else:
                backing = directory / (name+'-private')
                target.rename(backing)
                target.symlink_to(backing)
        copies.append(target)
    return MaintenanceOptions(copies[0], options.object_root, copies[1])


def marker_tool(directory: Path) -> tuple[Path, Path]:
    """测试专用工具若被调用即留下可计数标记，不能把快照等价说成未连接。"""
    marker, tool = directory / 'tool-calls', directory / 'rejected-pg-tool'
    tool.write_text('#!/bin/sh\nprintf called >> '+shlex.quote(str(marker))+'\nexit 99\n')
    tool.chmod(0o700)
    return tool, marker


def staging_snapshot(options: list[MaintenanceOptions], package: Path) -> dict[str, list[str]]:
    """只检查实际备份目标及源、目标对象根父目录，记录维护暂存名称清单。"""
    parents = {'backup_destination_parent': package.parent,
               'source_object_root_parent': options[0].object_root.parent,
               'restore_target_object_root_parent': options[1].object_root.parent}
    return {role: sorted(path.name for pattern in ['.finance-backup-*', '.finance-restore-*']
                         for path in parent.glob(pattern)) for role, parent in parents.items()}


async def bad_credentials(action: str, options: list[MaintenanceOptions],
                          dsns: list[str], directory: Path, package: Path) -> list[dict[str, Any]]:
    """四组真实CLI拒绝均核对两库全表、原密文、发布目录及PG工具零调用。"""
    tool, marker = marker_tool(directory)
    results = []
    before = [await database_snapshot(dsn, option.object_root)
              for dsn, option in zip(dsns, options, strict=True)]
    assert before[1] == ({}, {}) and not options[1].object_root.exists()
    for field in ['dsn', 'key']:
        for mode in ['0644', 'symlink']:
            name = action+'-'+field+'-'+mode
            selected = options[0 if action == 'backup' else 1]
            unsafe = private_copy(selected, directory / name, field, mode)
            destination = directory / (name+'-package') if action == 'backup' else package
            stages_before = staging_snapshot(options, destination)
            assert not any(stages_before.values())
            result = await actual_cli(action, unsafe, destination, tool)
            assert result['exit'] == 1
            assert result['output'] == {'status': 'failed', 'code': 'MAINTENANCE_INPUT_INVALID'}, {
                'actual': result, 'pg_tool_called': marker.exists()}
            after = [await database_snapshot(dsn, option.object_root)
                     for dsn, option in zip(dsns, options, strict=True)]
            assert before == after
            assert not options[1].object_root.exists() and not marker.exists()
            stages_after = staging_snapshot(options, destination)
            assert stages_before == stages_after and not any(stages_after.values())
            if action == 'backup':
                assert not destination.exists()
            results.append({'action': action, 'field': field, 'input': mode, **result,
                'source_all_tables_and_ciphertext_equal': before[0] == after[0],
                'empty_target_all_tables_and_ciphertext_equal': before[1] == after[1],
                'target_root_absent': not options[1].object_root.exists(),
                'package_unpublished': action == 'restore' or not destination.exists(),
                'temporary_stage_entries_before': stages_before,
                'temporary_stage_entries_after': stages_after,
                'temporary_stage_entries_equal': stages_before == stages_after,
                'temporary_stage_absent': not any(stages_after.values()), 'pg_tool_calls': 0,
                'database_connection_observation': 'not_inferred_from_snapshot'})
    return results


async def receipt_state(case: ProcessCase, doc: dict[str, Any]) -> dict[str, Any]:
    """原收件回执及import操作取自当前实际数据库，不手填任何工作流事实。"""
    row = await case.store.pool.fetchrow('SELECT * FROM finance_receipt WHERE id=$1',
                                       doc['content']['evidence_ids'][0])
    assert row is not None
    operation = await case.store.pool.fetchrow('SELECT * FROM finance_operation WHERE id=$1',
                                              row['operation_id'])
    assert operation is not None and operation['status'] == 'succeeded'
    return {'receipt': dict(row), 'operation': dict(operation)}


async def prepare_backup(case: ProcessCase, options: list[MaintenanceOptions], dsns: list[str],
                         directory: Path) -> dict[str, Any]:
    """先拒绝四组坏备份输入，恢复自身私密凭据后实际pg_dump并验证HMAC及认证。"""
    directory.mkdir()
    package = directory / 'valid-backup'
    rejected = await bad_credentials('backup', options, dsns, directory, package)
    before = await snapshot(case.store, options[0].object_root)
    dump, load = pg_tools(directory)
    backup = await actual_cli('backup', options[0], package, dump)
    assert backup['exit'] == 0 and backup['output']['backup_complete'] is True
    assert backup['output']['authenticated_objects'] == 1
    key = options[0].key_file.read_bytes()
    manifest = verify_manifest(package, key)
    assert manifest['complete_object_validation'] is True
    assert all(path.read_bytes() != key for path in package.rglob('*') if path.is_file())
    assert not any(path.name in {'key', 'dsn', 'aes'} for path in package.rglob('*'))
    assert before == await snapshot(case.store, options[0].object_root)
    backup_files = (await database_snapshot(dsns[0], package/'objects'))[1]
    assert before[1] == backup_files
    rejected += await bad_credentials('restore', options, dsns, directory, package)
    restore = await actual_cli('restore', options[1], package, load)
    assert restore['exit'] == 0 and restore['output']['database_restored'] is True
    assert restore['output']['workers_started'] is False
    assert restore['output']['external_side_effects'] == 0
    restored = await database_snapshot(dsns[1], options[1].object_root)
    assert before == restored == await snapshot(case.store, options[0].object_root)
    return {'bad_credentials': rejected, 'backup': backup, 'restore': restore,
        'manifest_hmac_valid': True, 'key_not_copied': True,
        'source_unchanged': True, 'all_public_tables_equal_before_start': before[0] == restored[0],
        'source_backup_target_ciphertexts_equal': before[1] == backup_files == restored[1]}


@asynccontextmanager
async def target_store(case: ProcessCase, options: MaintenanceOptions,
                       dsn: str) -> AsyncIterator[dict[str, Any]]:
    """恢复目标配置和池，所有后续审批取证走目标库，结束先关目标池再交还原夹具。"""
    source_store = case.store
    source_database = await source_store.pool.fetchval('SELECT current_database()')
    case.config.database_dsn_file = options.database_dsn_file
    case.config.objects.root = options.object_root
    case.config.objects.key_file = options.key_file
    write_config(case.path, case.config)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    assert pool is not None
    case.store = FinanceStore(pool)
    try:
        database = await case.store.pool.fetchval('SELECT current_database()')
        assert database != source_database
        yield {'source_database': source_database, 'target_database': database,
               'different_databases': database != source_database}
    finally:
        await pool.close()
        case.store = source_store


async def original_identity(case: ProcessCase, doc: dict[str, Any]) -> dict[str, Any]:
    """复用F13对象定位并核对实际原件AAD、对象键、SHA及原收件操作。"""
    leaf, summary = await receipt_ciphertext(case, doc)
    state = await receipt_state(case, doc)
    row = state['receipt']
    objects = EncryptedLocalObjects(case.config.objects.root, case.config.objects.key_file.read_bytes())
    assert await objects.get(row['object_key']) == case.raw
    assert row['sha256'] == hashlib.sha256(case.raw).hexdigest()
    assert leaf.read_bytes()
    return {**summary, 'object_key': row['object_key'], 'cipher_authenticated': True}

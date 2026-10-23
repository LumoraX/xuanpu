"""停机维护的独立数据库与加密原件配套备份恢复。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg

from xuanpu_finance.maintenance_files import (
    copy_objects,
    file_manifest,
    manifest_signature,
    verify_manifest,
)
from xuanpu_finance.maintenance_objects import authenticate_objects
from xuanpu_finance.maintenance_types import (
    WORKER_LOCK,
    MaintenanceError,
    MaintenanceLock,
    MaintenanceOptions,
    pg_environment,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path
    from typing import Any

__all__ = ['MaintenanceError', 'MaintenanceOptions', 'backup', 'restore']


@asynccontextmanager
async def maintenance_session(
    options: MaintenanceOptions, *, credentials: tuple[str, bytes] | None = None,
) -> AsyncIterator[tuple[MaintenanceLock, str, bytes]]:
    """维护锁贯穿全部步骤，凭据仅从显式文件读取且不回显。"""
    if credentials is None:
        dsn = (await asyncio.to_thread(options.database_dsn_file.read_text)).strip()
        key = await asyncio.to_thread(options.key_file.read_bytes)
    else:
        dsn, key = credentials
    if len(key) != 32:
        raise MaintenanceError('KEY_INVALID')
    try:
        conn = await asyncpg.connect(dsn)
    except (asyncpg.PostgresError, OSError, ValueError) as error:
        raise MaintenanceError('DATABASE_UNAVAILABLE') from error
    try:
        if not await conn.fetchval('SELECT pg_try_advisory_lock($1::bigint)', WORKER_LOCK):
            raise MaintenanceError('INSTANCE_ACTIVE')
        guard = MaintenanceLock(conn)
        yield guard, dsn, key
    finally:
        await conn.close()


async def backup(options: MaintenanceOptions, destination: Path, *, pg_dump: str = 'pg_dump') -> dict[str, Any]:
    """只有独立DB和实际原件均校验完成才原子发布完整备份目录。"""
    if destination.exists() or destination.is_symlink():
        raise MaintenanceError('DESTINATION_EXISTS')
    await asyncio.to_thread(validate_backup_paths, options, destination)
    async with maintenance_session(options) as (guard, dsn, key):
        conn = guard.connection
        if not await conn.fetchval("SELECT to_regclass('public.finance_company') IS NOT NULL"):
            raise MaintenanceError('FINANCE_DATABASE_REQUIRED')
        inventory = await authenticate_objects(conn, options.object_root, key)
        await guard.check()
        temporary = await asyncio.to_thread(tempfile.mkdtemp, prefix='.finance-backup-', dir=destination.parent)
        from pathlib import Path
        stage = Path(temporary)
        try:
            stream = await asyncio.to_thread((stage/'database.dump').open, 'wb')
            try:
                await guard.run([pg_dump, '--format=custom', '--no-owner', '--no-acl'], pg_environment(dsn), stdout=stream)
            finally:
                await asyncio.to_thread(stream.close)
            await guard.check()
            await asyncio.to_thread(copy_objects, options.object_root, stage/'objects')
            await guard.check()
            if await authenticate_objects(conn, stage/'objects', key) != inventory:
                raise MaintenanceError('BACKUP_OBJECT_INVENTORY_CHANGED')
            manifest = {'format_version': 1, 'database_schema': 'public', 'created_at': datetime.now(UTC).isoformat(), 'key_fingerprint': hashlib.sha256(key).hexdigest(), 'files': await asyncio.to_thread(file_manifest, stage), **inventory}
            manifest['manifest_hmac_sha256'] = manifest_signature(manifest, key)
            await asyncio.to_thread((stage/'manifest.json').write_text, json.dumps(manifest, ensure_ascii=False, indent=2))
            await guard.check()
            if destination.exists() or destination.is_symlink():
                raise MaintenanceError('DESTINATION_EXISTS')
            await asyncio.to_thread(stage.rename, destination)
            return {'backup_complete': inventory['complete_object_validation'], **inventory, 'created_at': manifest['created_at'], 'external_archive_backup': 'not_included'}
        finally:
            if stage.exists():
                await asyncio.to_thread(shutil.rmtree, stage)


async def empty_restore_target(conn: asyncpg.Connection) -> str:
    """拒绝既有库或非明确恢复命名，绝不覆盖生产财务库。"""
    name = str(await conn.fetchval('SELECT current_database()'))
    if not name.startswith(('finance_restore', 'finance_test')):
        raise MaintenanceError('RESTORE_DATABASE_NAME_REQUIRED')
    count = await conn.fetchval("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S','f')")
    schemas = await conn.fetchval("SELECT count(*) FROM pg_namespace WHERE nspname NOT IN ('public','pg_catalog','information_schema') AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp%'")
    if count or schemas:
        raise MaintenanceError('RESTORE_DATABASE_NOT_EMPTY')
    return name


async def restore(options: MaintenanceOptions, source: Path, *, pg_restore: str = 'pg_restore') -> dict[str, Any]:
    """仅恢复新空库与新对象根，实际解密核对后发布；不启动任何业务执行器。"""
    if options.object_root.exists() or options.object_root.is_symlink():
        raise MaintenanceError('RESTORE_OBJECT_ROOT_EXISTS')
    async with maintenance_session(options) as (guard, dsn, key):
        manifest = await asyncio.to_thread(verify_manifest, source, key)
        name = await empty_restore_target(guard.connection)
        from pathlib import Path
        stage = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix='.finance-restore-', dir=options.object_root.parent))
        database_attempted = False
        try:
            await asyncio.to_thread(copy_objects, source/'objects', stage/'objects')
            await guard.check()
            stream = await asyncio.to_thread((source/'database.dump').open, 'rb')
            try:
                database_attempted = True
                await guard.run([pg_restore, '--single-transaction', '--exit-on-error', '--no-owner', '--no-acl', '--dbname', name], pg_environment(dsn), stdin=stream, stdout=asyncio.subprocess.DEVNULL)
            finally:
                await asyncio.to_thread(stream.close)
            inventory = await authenticate_objects(guard.connection, stage/'objects', key)
            if any(inventory[k] != manifest[k] for k in inventory):
                raise MaintenanceError('RESTORE_OBJECT_INVENTORY_MISMATCH')
            await guard.check()
            if options.object_root.exists() or options.object_root.is_symlink():
                raise MaintenanceError('RESTORE_OBJECT_ROOT_EXISTS')
            await asyncio.to_thread((stage/'objects').rename, options.object_root)
            return {'database_restored': True, 'objects_published': True, 'workers_started': False, 'external_side_effects': 0, **inventory}
        except Exception as error:
            if database_attempted:
                raise MaintenanceError('RESTORE_FAILED_DATABASE_MAY_BE_COMMITTED') from error
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, stage)


def validate_backup_paths(options: MaintenanceOptions, destination: Path) -> None:
    """敏感输入或备份输出不能位于对象根内，同文件硬链接也不能混入。"""
    root = options.object_root.resolve()
    inputs = [options.database_dsn_file.resolve(), options.key_file.resolve()]
    if any(path.is_relative_to(root) for path in inputs) or destination.resolve().is_relative_to(root):
        raise MaintenanceError('SENSITIVE_PATH_OVERLAP')
    if root.exists():
        for path in root.rglob('*'):
            if path.is_file() and any(path.samefile(secret) for secret in inputs):
                raise MaintenanceError('SENSITIVE_PATH_OVERLAP')

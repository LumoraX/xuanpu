"""F11停机只读核验真实数据库、密文、互斥与凭据快照。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from xuanpu_finance.maintenance import MaintenanceError, MaintenanceOptions
from xuanpu_finance.objects import EncryptedLocalObjects

from .f11_verify_helpers import original, snapshot, verify
from .f11_verify_helpers import verify_database as verify_database  # noqa: F401
from .test_f4_maintenance import maintenance_database as maintenance_database  # noqa: F401
from .test_worker import ref


async def test_verify_real_pdf_is_readonly(verify_database: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实PDF实际认证且数据库和密文完全不变，不触及写入或工具。"""
    store, options, _, _ = verify_database
    await original(verify_database)
    before = await snapshot(store, options[0].object_root)
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        """所有写入边界及子进程在verify期间必须不可达。"""
        raise AssertionError('verification reached write boundary')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'mkdir', forbidden)
        patch.setattr(Path, 'write_bytes', forbidden)
        patch.setattr(Path, 'write_text', forbidden)
        patch.setattr(asyncio, 'create_subprocess_exec', forbidden)
        patch.setattr(type(store), 'migrate', forbidden)
        patch.setattr(type(store), 'configure', forbidden)
        report = await verify(options[0])
    assert report == {'status': 'completed', 'verification_status': 'complete', 'profile': 'internal_records', 'production_status': 'not_verified', 'authenticated_objects': 1, 'prepared_missing_count': 0, 'unmapped_objects_count': 0, 'complete_object_validation': True, 'workers_started': False, 'business_writes': 0}
    assert await snapshot(store, options[0].object_root) == before


async def test_initialized_empty_objects_is_complete(verify_database: Any) -> None:
    """已初始化库与既有空根可以核对零项，但不证明密钥正确。"""
    _, options, _, _ = verify_database
    options[0].object_root.mkdir()
    options[0].key_file.write_bytes(b'x'*32)
    report = await verify(options[0])
    assert report['verification_status'] == 'complete' and report['authenticated_objects'] == 0


@pytest.mark.parametrize('kind', ['missing_root', 'symlink_root', 'nested_symlink', 'wrong_key', 'corrupt', 'required_missing', 'hash_mismatch', 'size_mismatch'])
async def test_unsafe_or_broken_objects_fail(verify_database: Any, tmp_path: Path, kind: str) -> None:
    """缺根、链接、密钥、AAD认证及明文责任索引异常均不可返回部分成功。"""
    store, options, _, _ = verify_database
    await original(verify_database)
    root = options[0].object_root
    cipher = next(root.rglob('*.enc'))
    if kind == 'missing_root':
        options[0] = MaintenanceOptions(options[0].database_dsn_file, tmp_path/'absent', options[0].key_file)
    elif kind == 'symlink_root':
        link = tmp_path/'linked'; link.symlink_to(root, target_is_directory=True)
        options[0] = MaintenanceOptions(options[0].database_dsn_file, link, options[0].key_file)
    elif kind == 'nested_symlink':
        (root/'outside').symlink_to(options[0].key_file)
    elif kind == 'wrong_key':
        options[0].key_file.write_bytes(b'x'*32)
    elif kind == 'corrupt':
        raw = cipher.read_bytes(); cipher.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
    elif kind == 'required_missing':
        cipher.unlink()
    elif kind == 'hash_mismatch':
        await store.pool.execute("UPDATE finance_receipt SET sha256=repeat('0',64)")
    else:
        await store.pool.execute('UPDATE finance_item SET size_bytes=size_bytes+1')
    with pytest.raises(MaintenanceError):
        await verify(options[0])
    assert not (tmp_path/'absent').exists()


@pytest.mark.parametrize('kind', ['blank', 'general', 'unknown_schema', 'unknown_table', 'unknown_view', 'missing_column', 'wrong_type', 'non_public'])
async def test_only_initialized_public_internal_database(verify_database: Any, kind: str) -> None:
    """空白/通用库与未知结构均拒绝，不能靠F9空库放行。"""
    store, options, _, owner = verify_database
    options[0].object_root.mkdir()
    if kind == 'blank':
        options[0] = MaintenanceOptions(options[1].database_dsn_file, options[0].object_root, options[0].key_file)
    elif kind == 'general':
        await store.accept_import(owner, 'e', [ref('general', b'%PDF')], 'general')
    elif kind == 'unknown_schema':
        await store.pool.execute('CREATE SCHEMA unknown')
    elif kind == 'unknown_table':
        await store.pool.execute('CREATE TABLE unrelated(id int)')
    elif kind == 'unknown_view':
        await store.pool.execute('CREATE VIEW unexpected AS SELECT 1 AS id')
    elif kind == 'missing_column':
        await store.pool.execute('ALTER TABLE finance_item DROP COLUMN content_type')
    elif kind == 'wrong_type':
        await store.pool.execute('ALTER TABLE finance_item ALTER COLUMN content_type TYPE varchar(32)')
    else:
        await store.pool.execute('ALTER ROLE CURRENT_USER SET search_path=pg_catalog')
    try:
        with pytest.raises(MaintenanceError, match='INTERNAL_DATABASE_INCOMPATIBLE'):
            await verify(options[0])
    finally:
        if kind == 'non_public':
            await store.pool.execute('ALTER ROLE CURRENT_USER RESET search_path')


@pytest.mark.parametrize('kind', ['prepared', 'unmapped'])
async def test_incomplete_projects_counts_only(verify_database: Any, kind: str) -> None:
    """prepared缺件或只有合法头的额外密文只能标incomplete，不算实际认证。"""
    store, options, _, _ = verify_database
    await original(verify_database)
    if kind == 'prepared':
        await store.pool.execute('DELETE FROM finance_receipt')
        await store.pool.execute('UPDATE finance_item SET receipt_id=NULL')
        next(options[0].object_root.rglob('*.enc')).unlink()
    else:
        path = EncryptedLocalObjects(options[0].object_root, b'k'*32)._path('secret/unmapped')
        path.parent.mkdir(exist_ok=True); path.write_bytes(b'XF01'+b'x'*28)
    report = await verify(options[0])
    assert report['verification_status'] == 'incomplete'
    assert report['complete_object_validation'] is False
    assert report['prepared_missing_count'] == (kind == 'prepared')
    assert report['unmapped_objects_count'] == (kind == 'unmapped')
    assert 'secret' not in str(report) and 'private-source' not in str(report)


async def test_active_instance_rejected(verify_database: Any) -> None:
    """维护与runtime同号独占锁，活实例下不能核验。"""
    _, options, dsns, _ = verify_database
    options[0].object_root.mkdir()
    conn = await asyncpg.connect(dsns[0])
    await conn.execute('SELECT pg_advisory_lock(617472002)')
    try:
        with pytest.raises(MaintenanceError, match='INSTANCE_ACTIVE'):
            await verify(options[0])
    finally:
        await conn.close()


@pytest.mark.parametrize('kind', ['dsn_symlink', 'key_symlink', 'dsn_permissions', 'key_permissions', 'key_long', 'dsn_long', 'query', 'not_pg', 'fifo'])
async def test_credentials_checked_before_connect(verify_database: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """固定文件有界私密检查失败必须在首个数据库连接之前拒绝。"""
    import os
    _, options, _, _ = verify_database
    target = options[0].database_dsn_file if kind.startswith('dsn') or kind in {'query', 'not_pg'} else options[0].key_file
    if kind.endswith('symlink'):
        moved = tmp_path/'moved'; target.rename(moved); target.symlink_to(moved)
    elif kind.endswith('permissions'):
        target.chmod(0o644)
    elif kind.endswith('long'):
        target.write_bytes(b'x'*65537)
    elif kind == 'query':
        target.write_text(target.read_text()+'?sslmode=require')
    elif kind == 'not_pg':
        target.write_text('https://private:password@host/db')
    else:
        target.unlink(); os.mkfifo(target)
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        """坏凭据不能尝试连接驱动。"""
        raise AssertionError('invalid credentials connected')
    monkeypatch.setattr(asyncpg, 'connect', forbidden)
    with pytest.raises(MaintenanceError):
        await verify(options[0])


async def test_captured_credentials_are_not_reopened(verify_database: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """捕获后替换文件，维护连接和解密仍消费同一已验DSN/key。"""
    _, options, dsns, _ = verify_database
    await original(verify_database)
    connect = asyncpg.connect
    calls = []
    async def replace_after_capture(dsn: str, **kwargs: Any) -> Any:
        """在首个外部连接前替换秘密文件，证明后续不重读。"""
        calls.append(dsn)
        options[0].database_dsn_file.write_text(dsns[1])
        options[0].key_file.write_bytes(b'x'*32)
        return await connect(dsn, **kwargs)
    monkeypatch.setattr(asyncpg, 'connect', replace_after_capture)
    report = await verify(options[0])
    assert calls == [dsns[0]] and report['authenticated_objects'] == 1


@pytest.mark.parametrize('kind', ['unlock', 'terminate', 'query'])
async def test_lock_loss_and_query_errors_are_fatal(verify_database: Any, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """真实解锁、连接终止和PG查询失败都不能发布部分结果。"""
    import xuanpu_finance.maintenance_verify as module
    _, options, dsns, _ = verify_database
    await original(verify_database)
    authenticate = module.authenticate_objects
    async def interrupted(conn: Any, root: Path, key: bytes) -> dict[str, Any]:
        """在实际核验之后真实破坏锁或执行失败查询。"""
        inventory = await authenticate(conn, root, key)
        if kind == 'unlock':
            await conn.execute('SELECT pg_advisory_unlock(617472002)')
        elif kind == 'terminate':
            other = await asyncpg.connect(dsns[0])
            try:
                await other.execute('SELECT pg_terminate_backend($1)', conn.get_server_pid())
            finally:
                await other.close()
        else:
            await conn.fetch('SELECT * FROM intentionally_absent_table')
        return inventory
    monkeypatch.setattr(module, 'authenticate_objects', interrupted)
    with pytest.raises(MaintenanceError, match='MAINTENANCE_LOCK_LOST|VERIFICATION_FAILED'):
        await verify(options[0])


async def test_all_checks_share_readonly_repeatable_read_and_lock(verify_database: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """结构与密文核验共用同一个只读可重复读事务和持锁连接。"""
    import xuanpu_finance.maintenance_verify as module
    _, options, _, _ = verify_database
    await original(verify_database)
    original_check = module.check_verification_database
    authenticate = module.authenticate_objects
    observed = []
    async def inspect(conn: Any) -> None:
        """读取真实PG事务属性和实际锁，拒绝模拟成功。"""
        observed.append(conn.get_server_pid())
        assert await conn.fetchval('SHOW transaction_read_only') == 'on'
        assert await conn.fetchval('SHOW transaction_isolation') == 'repeatable read'
        assert await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() AND objid=617472002)")
    async def check(conn: Any) -> None:
        """结构扫描前核对事务和锁。"""
        await inspect(conn)
        await original_check(conn)
    async def objects(conn: Any, root: Path, key: bytes) -> dict[str, Any]:
        """认证前核对仍为相同的事务和锁。"""
        await inspect(conn)
        return await authenticate(conn, root, key)
    monkeypatch.setattr(module, 'check_verification_database', check)
    monkeypatch.setattr(module, 'authenticate_objects', objects)
    await verify(options[0])
    assert len(observed) == 2 and observed[0] == observed[1]

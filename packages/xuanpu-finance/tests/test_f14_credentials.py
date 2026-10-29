"""F14维护输入安全、一次快照及有限CLI异常输出。"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from contextlib import chdir
from pathlib import Path
from typing import Any

import pytest
from xuanpu_finance.maintenance import MaintenanceError, backup, maintenance_session
from xuanpu_finance.maintenance_types import pg_environment

from .f14_credentials_helpers import (
    SAFE_DSN,
    SAFE_KEY,
    Connection,
    forbidden_boundaries,
    invalidate,
    invoke,
    options_for,
)


@pytest.mark.parametrize('action', ['session', 'backup', 'restore'])
@pytest.mark.parametrize('field', ['dsn', 'key'])
@pytest.mark.parametrize('kind', ['permissions', 'symlink', 'directory', 'empty', 'oversize'])
async def test_invalid_files_fail_before_all_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, field: str, kind: str,
) -> None:
    """不安全凭据在首个连接、工具或暂存之前拒绝且不改目标字节。"""
    options = options_for(tmp_path)
    retained = tmp_path/'retained'
    retained.write_bytes(b'unchanged-public-test-bytes')
    invalidate(options.database_dsn_file if field == 'dsn' else options.key_file, kind)
    calls = forbidden_boundaries(monkeypatch)
    with pytest.raises(MaintenanceError, match='^MAINTENANCE_INPUT_INVALID$'):
        await invoke(action, options, tmp_path/'package')
    assert calls == {'connect': 0, 'tool': 0, 'stage': 0}
    assert retained.read_bytes() == b'unchanged-public-test-bytes'
    assert not options.object_root.exists() and not (tmp_path/'package').exists()
    assert not list(tmp_path.glob('.finance-*'))


@pytest.mark.parametrize('action', ['session', 'backup', 'restore'])
@pytest.mark.parametrize('kind,code', [('utf8', 'MAINTENANCE_INPUT_INVALID'), ('short', 'KEY_INVALID')])
async def test_decode_and_key_length_have_stable_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, kind: str, code: str,
) -> None:
    """坏UTF8和合法私密文件内的错长度密钥保留各自稳定失败码。"""
    options = options_for(tmp_path)
    invalidate(options.database_dsn_file if kind == 'utf8' else options.key_file, kind)
    calls = forbidden_boundaries(monkeypatch)
    with pytest.raises(MaintenanceError, match='^'+code+'$'):
        await invoke(action, options, tmp_path/'package')
    assert calls == {'connect': 0, 'tool': 0, 'stage': 0}
    assert not options.object_root.exists() and not (tmp_path/'package').exists()


@pytest.mark.parametrize('action', ['session', 'backup', 'restore'])
@pytest.mark.parametrize('field', ['dsn', 'key'])
@pytest.mark.parametrize('kind', ['fifo', 'socket'])
async def test_special_files_are_bounded_in_killable_process(
    tmp_path: Path, action: str, field: str, kind: str, request: pytest.FixtureRequest,
) -> None:
    """FIFO和socket在独立可杀死进程三秒内拒绝，父pytest没有阻塞线程。"""
    options = options_for(tmp_path)
    path = options.database_dsn_file if field == 'dsn' else options.key_file
    path.unlink()
    if kind == 'fifo':
        os.mkfifo(path, 0o600)
    else:
        with chdir(tmp_path), socket.socket(socket.AF_UNIX) as endpoint:
            endpoint.bind(path.name)
        path.chmod(0o600)
    helper = Path(__file__).with_name('f14_credentials_helpers.py')
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(helper), str(options.database_dsn_file), str(options.key_file),
        str(options.object_root), action, str(tmp_path/'package'),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert process.returncode == 0 and stderr == b''
    assert json.loads(stdout) == {'code': 'MAINTENANCE_INPUT_INVALID', 'calls': {'connect': 0, 'tool': 0, 'stage': 0}}
    assert not options.object_root.exists() and not (tmp_path/'package').exists()
    request.node.user_properties.append(('f14_credentials', json.dumps({'kind': kind, 'field': field, 'action': action, 'bounded_seconds': 3, 'connections': 0, 'tools': 0, 'stages': 0})))


@pytest.mark.parametrize('mode', [0o400, 0o600])
async def test_private_regular_files_are_accepted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int,
) -> None:
    """0400和0600普通凭据合法，连接准确消费规范化DSN和32字节key。"""
    import xuanpu_finance.maintenance as module
    options = options_for(tmp_path, mode)
    connection = Connection()
    dsns = []

    async def connect(dsn: str) -> Connection:
        """记录驱动实际接收的单次捕获值。"""
        dsns.append(dsn)
        return connection

    monkeypatch.setattr(module.asyncpg, 'connect', connect)
    async with maintenance_session(options) as (_, dsn, key):
        assert dsn == SAFE_DSN and key == SAFE_KEY
    assert dsns == [SAFE_DSN] and connection.closed


@pytest.mark.parametrize('query,code', [
    ('?application_name=x', 'DATABASE_DSN_OPTIONS_UNSUPPORTED'),
    ('?sslmode=require&sslmode=disable', 'DATABASE_DSN_OPTIONS_UNSUPPORTED'),
    ('?broken', 'MAINTENANCE_INPUT_INVALID'),
    (':badport', 'MAINTENANCE_INPUT_INVALID'),
    ('not-postgres', 'DATABASE_DSN_INVALID'),
])
async def test_maintenance_dsn_rejected_before_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str, code: str,
) -> None:
    """维护既有DSN白名单和格式异常都在连接前转换为有限错误。"""
    options = options_for(tmp_path)
    value = SAFE_DSN+query
    if query == ':badport':
        value = SAFE_DSN.replace(':5432', ':badport')
    elif query == 'not-postgres':
        value = 'https://localhost/test'
    options.database_dsn_file.write_text(value)
    calls = forbidden_boundaries(monkeypatch)
    with pytest.raises(MaintenanceError, match='^'+code+'$'):
        await invoke('session', options, tmp_path/'package')
    assert calls == {'connect': 0, 'tool': 0, 'stage': 0}


@pytest.mark.parametrize('action', ['backup', 'restore', 'verify'])
@pytest.mark.parametrize('known', [False, True])
def test_cli_failure_has_only_finite_code(
    monkeypatch: pytest.MonkeyPatch, capsys: Any, action: str, known: bool,
) -> None:
    """未知故障隐藏安全测试哨兵，已知恢复可能提交等错误码原样保留。"""
    import xuanpu_finance.maintenance_cli as module

    async def broken(*args: Any, **kwargs: Any) -> Any:
        """异常正文只用公开哨兵，不在失败证据中注入真实秘密。"""
        if known:
            raise MaintenanceError('RESTORE_FAILED_DATABASE_MAY_BE_COMMITTED')
        raise RuntimeError('public-f14-secret-path-traceback-sentinel')

    name = 'verify_internal_records' if action == 'verify' else action
    monkeypatch.setattr(module, name, broken)
    args = ['maintenance', action, '--database-dsn-file', '/public-dsn', '--objects', '/public-objects', '--key-file', '/public-key']
    if action != 'verify':
        args.extend(['--package', '/public-package'])
    monkeypatch.setattr(sys, 'argv', args)
    with pytest.raises(SystemExit) as failure:
        module.main()
    assert failure.value.code == 1
    captured = capsys.readouterr()
    code = 'VERIFICATION_FAILED' if action == 'verify' else 'MAINTENANCE_FAILED'
    if known:
        code = 'RESTORE_FAILED_DATABASE_MAY_BE_COMMITTED'
    assert json.loads(captured.out) == {'status': 'failed', 'code': code}
    assert captured.err == '' and 'sentinel' not in captured.out


async def test_snapshot_shared_by_connection_tools_and_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """捕获后真实替换文件，驱动、PG环境和认证仍共用原单次快照。"""
    import xuanpu_finance.maintenance as module
    import xuanpu_finance.readiness.credentials as readers
    options = options_for(tmp_path)
    options.object_root.mkdir()
    original_reader = readers.read_fixed
    reads = []
    keys = []
    environments = []
    dsns = []
    connection = Connection()
    inventory = {'complete_object_validation': True}

    def read(path: Path, **kwargs: Any) -> Any:
        """统计真实读取次数，替换后不可重新打开秘密文件。"""
        reads.append(path)
        return original_reader(path, **kwargs)

    async def connect(dsn: str) -> Connection:
        """读取结束屏障处改文件，模拟管理员轮换。"""
        dsns.append(dsn)
        options.database_dsn_file.write_text('postgresql://replaced:replaced@localhost/other')
        options.key_file.write_bytes(b'x'*32)
        return connection

    async def authenticate(conn: Any, root: Path, key: bytes) -> dict[str, bool]:
        """记录备份前后实际认证所消费的捕获密钥。"""
        keys.append(key)
        return inventory

    async def run(guard: Any, args: list[str], env: dict[str, str], **kwargs: Any) -> None:
        """只记录工具环境，本单测不声明真实PG工具或TLS握手。"""
        environments.append(env)
        kwargs['stdout'].write(b'public-dump-placeholder')

    async def check(guard: Any) -> None:
        """协议替身允许每个持锁检查点。"""

    monkeypatch.setattr(readers, 'read_fixed', read)
    # 兼容私有捕获函数从既有模块直接导入安全读取函数。
    if hasattr(module, 'read_fixed'):
        monkeypatch.setattr(module, 'read_fixed', read)
    monkeypatch.setattr(module.asyncpg, 'connect', connect)
    monkeypatch.setattr(module, 'authenticate_objects', authenticate)
    monkeypatch.setattr(module.MaintenanceLock, 'run', run)
    monkeypatch.setattr(module.MaintenanceLock, 'check', check)
    report = await backup(options, tmp_path/'package')
    assert report['backup_complete'] is True
    assert reads == [options.database_dsn_file, options.key_file]
    assert dsns == [SAFE_DSN] and keys == [SAFE_KEY, SAFE_KEY]
    assert environments[0]['PGUSER'] == 'f14_user'
    assert environments[0]['PGPASSWORD'] == 'f14_password'
    assert connection.closed


async def test_injected_credentials_are_not_reopened_or_maintenance_revalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify已捕获凭据注入不读路径，也不附加维护DSN解析规则。"""
    import xuanpu_finance.maintenance as module
    options = options_for(tmp_path)
    options.database_dsn_file.unlink()
    options.key_file.unlink()
    connection = Connection()
    dsns = []
    injected = (SAFE_DSN+'?verify-injected-test-only=true', SAFE_KEY)

    async def connect(dsn: str) -> Connection:
        """注入分支直接消费上游已验证的不可变DSN。"""
        dsns.append(dsn)
        return connection

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        """注入分支不允许再次解析维护白名单。"""
        raise AssertionError('injected snapshot revalidated')

    monkeypatch.setattr(module.asyncpg, 'connect', connect)
    monkeypatch.setattr(module, 'pg_environment', forbidden)
    async with maintenance_session(options, credentials=injected) as (_, dsn, key):
        assert (dsn, key) == injected
    assert dsns == [injected[0]] and connection.closed


@pytest.mark.parametrize('failure', ['open', 'fstat', 'read'])
async def test_io_errors_are_normalized_and_open_descriptors_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """底层打开、元数据或读取故障归一，所有已打开描述符均被关闭。"""
    import xuanpu_finance.readiness.credentials as readers
    options = options_for(tmp_path)
    descriptors = []
    closed = []
    original_open = readers.os.open
    original_close = readers.os.close

    def opening(*args: Any, **kwargs: Any) -> int:
        """记录真实描述符，无凭据正文进入异常。"""
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def closing(descriptor: int) -> None:
        """调用真实关闭并记录资源释放。"""
        original_close(descriptor)
        closed.append(descriptor)

    def broken(*args: Any, **kwargs: Any) -> Any:
        """可控安全哨兵模拟底层IO故障。"""
        raise OSError('public-f14-io-sentinel')

    calls = forbidden_boundaries(monkeypatch)
    monkeypatch.setattr(readers.os, 'open', broken if failure == 'open' else opening)
    monkeypatch.setattr(readers.os, 'close', closing)
    if failure != 'open':
        monkeypatch.setattr(readers.os, failure, broken)
    with pytest.raises(MaintenanceError, match='^MAINTENANCE_INPUT_INVALID$'):
        await invoke('session', options, tmp_path/'package')
    assert descriptors == closed
    assert len(closed) == (failure != 'open')
    assert calls == {'connect': 0, 'tool': 0, 'stage': 0}


async def test_maintenance_tls_options_parse_without_claiming_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅证明维护TLS白名单和编码密码解析兼容，不代表实际TLS连接。"""
    import xuanpu_finance.maintenance as module
    options = options_for(tmp_path)
    dsn = 'postgresql://user:p%40ss%3Aword@localhost:5432/db?sslmode=verify-full&sslrootcert=%2Froot.pem&sslcert=%2Fcert.pem&sslkey=%2Fkey.pem'
    options.database_dsn_file.write_text(dsn+'\n')
    connection = Connection()
    seen = []

    async def connect(value: str) -> Connection:
        """协议替身只核对驱动参数，禁止冒称TLS握手成功。"""
        seen.append(value)
        return connection

    monkeypatch.setattr(module.asyncpg, 'connect', connect)
    async with maintenance_session(options) as (_, captured, key):
        env = pg_environment(captured)
        assert env['PGPASSWORD'] == 'p@ss:word' and key == SAFE_KEY
        assert [env[name] for name in ['PGSSLMODE', 'PGSSLROOTCERT', 'PGSSLCERT', 'PGSSLKEY']] == ['verify-full', '/root.pem', '/cert.pem', '/key.pem']
    assert seen == [dsn] and connection.closed

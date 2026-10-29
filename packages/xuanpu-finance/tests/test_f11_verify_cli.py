"""F11真实维护CLI固定JSON、退出码与旧动作参数契约。"""
from __future__ import annotations

import json
from typing import Any

import pytest
from xuanpu_finance.objects import EncryptedLocalObjects

from .f11_verify_helpers import cli, original
from .f11_verify_helpers import verify_database as verify_database  # noqa: F401
from .test_f4_maintenance import maintenance_database as maintenance_database  # noqa: F401


async def test_verify_cli_complete_and_empty(verify_database: Any, request: pytest.FixtureRequest) -> None:
    """真实进程完整认证返回固定有限计数字段，空根也可核对零项。"""
    _, options, _, _ = verify_database
    options[0].object_root.mkdir()
    code, report, stderr = await cli(options[0])
    assert code == 0 and not stderr and report['authenticated_objects'] == 0
    await original(verify_database)
    code, report, stderr = await cli(options[0])
    assert code == 0 and not stderr and report['authenticated_objects'] == 1
    assert len(report) == 10 and report['verification_status'] == 'complete'
    request.node.user_properties.extend([('verify_complete_output', json.dumps(report, sort_keys=True)), ('verify_complete_exit', code)])


@pytest.mark.parametrize('kind', ['prepared', 'unmapped'])
async def test_verify_cli_incomplete(verify_database: Any, kind: str, request: pytest.FixtureRequest) -> None:
    """缺prepared或额外密文真实CLI退出2并且不输出敏感清单。"""
    store, options, _, _ = verify_database
    await original(verify_database)
    if kind == 'prepared':
        await store.pool.execute('DELETE FROM finance_receipt')
        await store.pool.execute('UPDATE finance_item SET receipt_id=NULL')
        next(options[0].object_root.rglob('*.enc')).unlink()
    else:
        path = EncryptedLocalObjects(options[0].object_root, b'k'*32)._path('secret/unmapped')
        path.parent.mkdir(exist_ok=True); path.write_bytes(b'XF01'+b'x'*28)
    code, report, stderr = await cli(options[0])
    assert code == 2 and not stderr and report['verification_status'] == 'incomplete'
    assert 'unmapped_objects' not in report and 'prepared_missing' not in report
    request.node.user_properties.extend([('verify_incomplete_output', json.dumps(report, sort_keys=True)), ('verify_incomplete_exit', code)])


@pytest.mark.parametrize('kind', ['missing', 'wrong_key', 'blank'])
async def test_verify_cli_failed_has_only_safe_code(verify_database: Any, kind: str, request: pytest.FixtureRequest) -> None:
    """必须原件异常及空白数据库只给固定失败码，不能输出部分成功。"""
    _, options, _, _ = verify_database
    await original(verify_database)
    if kind == 'missing':
        next(options[0].object_root.rglob('*.enc')).unlink()
    elif kind == 'wrong_key':
        options[0].key_file.write_bytes(b'x'*32)
    else:
        from xuanpu_finance.maintenance import MaintenanceOptions
        options[0] = MaintenanceOptions(options[1].database_dsn_file, options[0].object_root, options[0].key_file)
    code, report, stderr = await cli(options[0])
    assert code == 1 and not stderr
    assert set(report) == {'status', 'code'} and report['status'] == 'failed'
    request.node.user_properties.extend([('verify_failed_output', json.dumps(report, sort_keys=True)), ('verify_failed_exit', code)])


@pytest.mark.parametrize('extra', [('--package', '/private-package'), ('--pg-tool', '/private-tool')])
async def test_verify_cli_forbids_backup_options(verify_database: Any, extra: tuple[str, str]) -> None:
    """verify不接受任何包或PG工具参数。"""
    _, options, _, _ = verify_database
    code, report, stderr = await cli(options[0], *extra)
    assert code == 2 and not report and stderr


def test_verify_cli_unexpected_internal_failure_is_safe(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """未知内部错误也必须失败且隐藏异常正文，绝不能伪装完成。"""
    import sys

    import xuanpu_finance.maintenance_cli as module
    async def broken(options: Any) -> Any:
        """模拟未知程序故障，异常正文故意携带秘密哨兵。"""
        raise RuntimeError('secret-sentinel-internal-failure')
    monkeypatch.setattr(module, 'verify_internal_records', broken)
    monkeypatch.setattr(sys, 'argv', ['maintenance', 'verify', '--database-dsn-file', '/dsn', '--objects', '/objects', '--key-file', '/key'])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == '{"status": "failed", "code": "VERIFICATION_FAILED"}'
    assert not captured.err


@pytest.mark.parametrize('action', ['backup', 'restore'])
async def test_backup_restore_cli_still_require_package(verify_database: Any, action: str) -> None:
    """旧备份恢复动作缺包参数仍由解析器拒绝。"""
    _, options, _, _ = verify_database
    code, report, stderr = await cli(options[0], action=action)
    assert code == 2 and not report and 'package' in stderr


async def test_backup_restore_cli_pg_tool_compatibility(verify_database: Any, tmp_path: Any) -> None:
    """真实CLI仍调用显式成熟PG工具生成并恢复配套备份。"""
    from .test_f4_maintenance import pg_tools
    _, options, _, _ = verify_database
    await original(verify_database)
    dump, load = pg_tools(tmp_path)
    package = tmp_path/'backup-package'
    code, report, stderr = await cli(options[0], '--package', str(package), '--pg-tool', str(dump), action='backup')
    assert code == 0 and not stderr and report['backup_complete'] is True
    code, report, stderr = await cli(options[1], '--package', str(package), '--pg-tool', str(load), action='restore')
    assert code == 0 and not stderr and report['database_restored'] is True

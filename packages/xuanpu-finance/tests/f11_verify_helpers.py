"""F11真实独立库、PDF原件及只读状态快照辅助。"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from typing import TYPE_CHECKING, Any

import pytest_asyncio
from xuanpu_finance.maintenance import MaintenanceOptions
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.storage import FinanceStore

from .test_f7_records import configured, source

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

VerifyDatabase = tuple[FinanceStore, list[MaintenanceOptions], list[str], FinancePrincipal]


@pytest_asyncio.fixture
async def verify_database(maintenance_database: Any) -> AsyncIterator[VerifyDatabase]:
    """只调整本轮专用key权限，复用F4两独立数据库夹具。"""
    store, options, dsns = maintenance_database
    options[0].key_file.chmod(0o600)
    owner, _ = await configured(store)
    yield store, options, dsns, owner


async def verify(options: MaintenanceOptions) -> dict[str, Any]:
    """通过公开维护验证入口调用，缺功能以清晰断言报红。"""
    name = 'xuanpu_finance.maintenance_verify'
    assert importlib.util.find_spec(name) is not None, 'F11独立verify尚未实现'
    return await importlib.import_module(name).verify_internal_records(options)


async def original(database: VerifyDatabase) -> bytes:
    """既有真实worker保全有效PDF及责任索引。"""
    store, options, _, owner = database
    return (await source(store, owner, EncryptedLocalObjects(options[0].object_root, b'k'*32), 'private-source'))[1]


async def snapshot(store: Any, root: Path) -> tuple[Any, Any]:
    """逐表持久内容和逐文件密文在核验前后精确比对。"""
    names = await store.pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    rows = {}
    for row in names:
        table = '"' + row['tablename'].replace('"', '""') + '"'
        rows[row['tablename']] = [r['body'] for r in await store.pool.fetch('SELECT to_jsonb(t)::text AS body FROM '+table+' t ORDER BY to_jsonb(t)::text')]
    files = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    return rows, files


async def cli(options: MaintenanceOptions, *extra: str, action: str = 'verify') -> tuple[int, dict[str, Any], str]:
    """启动真实模块进程，只捕获有界脱敏输出。"""
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'xuanpu_finance.maintenance_cli', action, '--database-dsn-file', str(options.database_dsn_file), '--objects', str(options.object_root), '--key-file', str(options.key_file), *extra, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    assert len(stdout) < 2048 and len(stderr) < 4096
    assert options.database_dsn_file.read_text() not in (stdout+stderr).decode()
    assert options.key_file.read_bytes().decode() not in (stdout+stderr).decode()
    assert process.returncode is not None
    return process.returncode, json.loads(stdout) if stdout else {}, stderr.decode()

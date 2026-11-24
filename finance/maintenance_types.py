"""独立维护参数、成熟PG工具调用及全程独占锁。"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

import asyncpg

if TYPE_CHECKING:
    from pathlib import Path

WORKER_LOCK = 617472002


class MaintenanceError(Exception):
    """仅向运维输出稳定错误代码，避免原始数据库或工具错误泄密。"""


@dataclass(frozen=True)
class MaintenanceOptions:
    """显式注入独立数据库、local密文目录及另行保全的密钥。"""

    database_dsn_file: Path
    object_root: Path
    key_file: Path


class MaintenanceLock:
    """与运行服务互斥，持锁连接丢失后禁止发布任何完整备份。"""

    def __init__(self, connection: asyncpg.Connection) -> None:
        """只接受当前维护专属连接，不能借用业务连接池会话锁。"""
        self.connection = connection
        self.lost = asyncio.Event()
        connection.add_termination_listener(self._terminated)

    def _terminated(self, connection: object) -> None:
        """连接关闭立即唤醒子进程终止逻辑。"""
        self.lost.set()

    async def check(self) -> None:
        """每个检查点重新核对当前会话仍实际持有独占锁。"""
        if self.lost.is_set() or self.connection.is_closed():
            raise MaintenanceError('MAINTENANCE_LOCK_LOST')
        try:
            held = await self.connection.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() AND classid=0 AND objid=$1)", WORKER_LOCK)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as error:
            raise MaintenanceError('MAINTENANCE_LOCK_LOST') from error
        if not held:
            raise MaintenanceError('MAINTENANCE_LOCK_LOST')

    async def run(self, args: list[str], env: dict[str, str], **streams: Any) -> None:
        """调用成熟工具，丢锁或取消时终止并等待子进程，忽略敏感stderr正文。"""
        await self.check()
        try:
            process = await asyncio.create_subprocess_exec(*args, env=env, stderr=asyncio.subprocess.PIPE, **streams)
        except OSError as error:
            raise MaintenanceError('PG_TOOL_UNAVAILABLE') from error
        done = asyncio.create_task(process.communicate())
        lost = asyncio.create_task(self.lost.wait())
        try:
            await asyncio.wait([done, lost], return_when=asyncio.FIRST_COMPLETED)
            if self.lost.is_set():
                raise MaintenanceError('MAINTENANCE_LOCK_LOST')
            await done
            if process.returncode:
                raise MaintenanceError('PG_TOOL_FAILED')
            await self.check()
        finally:
            lost.cancel()
            if process.returncode is None:
                process.terminate()
                await process.wait()
            if not done.done():
                done.cancel()
            await asyncio.gather(done, lost, return_exceptions=True)


def pg_environment(dsn: str) -> dict[str, str]:
    """凭据仅经子进程环境传入，命令行和清册不包含DSN或密码。"""
    parts = urlsplit(dsn)
    if parts.scheme not in {'postgres', 'postgresql'} or not parts.hostname or not parts.path[1:]:
        raise MaintenanceError('DATABASE_DSN_INVALID')
    env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
    values = {'PGHOST': parts.hostname, 'PGPORT': str(parts.port or 5432), 'PGDATABASE': unquote(parts.path[1:]), 'PGUSER': unquote(parts.username or ''), 'PGPASSWORD': unquote(parts.password or ''), 'PGCONNECT_TIMEOUT': '10'}
    query = parse_qs(parts.query, strict_parsing=True)
    allowed = {'sslmode': 'PGSSLMODE', 'sslrootcert': 'PGSSLROOTCERT', 'sslcert': 'PGSSLCERT', 'sslkey': 'PGSSLKEY'}
    if set(query) - set(allowed) or any(len(v) != 1 for v in query.values()):
        raise MaintenanceError('DATABASE_DSN_OPTIONS_UNSUPPORTED')
    values.update({allowed[k]: v[0] for k, v in query.items()})
    return {**env, **values}

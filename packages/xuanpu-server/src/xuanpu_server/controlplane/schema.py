"""控制面表结构的迁移。

做法与 taifeng-stores 的迁移一致（其 ADR 0004）：随包分发的 SQL 脚本按版本号顺序执行，
一次迁移一个事务，事务开头取咨询锁，已执行的脚本核对摘要。平台的表不是 taifeng 协议的
实现，不属于适配包，所以迁移逻辑在这里另有一份。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from string import Template
from typing import TYPE_CHECKING

import asyncpg
from taifeng_stores import SqlNamespace

from xuanpu_server.errors import SchemaError

if TYPE_CHECKING:
    from asyncpg.pool import PoolConnectionProxy

    Connection = asyncpg.Connection | PoolConnectionProxy

_SCRIPT_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LEDGER = "schema_migrations"

DEFAULT_NAMESPACE = SqlNamespace(schema="public", prefix="xp_")


@dataclass(frozen=True)
class Migration:
    """一个迁移脚本。"""

    version: int
    name: str
    sql_template: str

    @property
    def checksum(self) -> str:
        """脚本模板的摘要。"""
        return hashlib.sha256(self.sql_template.encode("utf-8")).hexdigest()


def load_migrations() -> list[Migration]:
    """读取全部迁移脚本，按版本号升序。"""
    directory = resources.files("xuanpu_server.controlplane").joinpath("migrations")
    found: list[Migration] = []
    for entry in directory.iterdir():
        matched = _SCRIPT_NAME.match(entry.name)
        if matched is not None:
            found.append(
                Migration(int(matched.group(1)), matched.group(2), entry.read_text("utf-8"))
            )
    found.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in found]
    if versions != list(range(1, len(versions) + 1)):
        raise SchemaError(f"迁移版本号不连续：{versions}")
    return found


def latest_version() -> int:
    """代码要求的表结构版本。"""
    migrations = load_migrations()
    return migrations[-1].version if migrations else 0


def _lock_key(namespace: SqlNamespace) -> int:
    """按命名空间派生咨询锁的键。"""
    digest = hashlib.sha256(f"xuanpu:{namespace.schema}.{namespace.prefix}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def _apply_pending(conn: Connection, namespace: SqlNamespace) -> list[Migration]:
    """在当前事务里执行尚未执行的迁移。"""
    ledger = namespace.table(_LEDGER)
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{namespace.schema}"')
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {ledger} ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    rows = await conn.fetch(f"SELECT version, checksum FROM {ledger}")
    applied = {int(row["version"]): str(row["checksum"]) for row in rows}
    executed: list[Migration] = []
    for migration in load_migrations():
        recorded = applied.get(migration.version)
        if recorded is not None:
            if recorded != migration.checksum:
                raise SchemaError(
                    f"迁移 {migration.version:04d} 已执行，但脚本内容与执行时不同；"
                    "已发布的迁移不得修改，请新增迁移脚本"
                )
            continue
        sql = Template(migration.sql_template).substitute(
            schema=namespace.schema, prefix=namespace.prefix
        )
        try:
            await conn.execute(sql)
        except asyncpg.PostgresError as exc:
            raise SchemaError(
                f"迁移 {migration.version:04d}_{migration.name} 执行失败：{exc}"
            ) from exc
        await conn.execute(
            f"INSERT INTO {ledger} (version, name, checksum) VALUES ($1, $2, $3)",
            migration.version,
            migration.name,
            migration.checksum,
        )
        executed.append(migration)
    return executed


async def apply_migrations(
    pool: asyncpg.Pool, namespace: SqlNamespace = DEFAULT_NAMESPACE
) -> list[Migration]:
    """把控制面的表迁移到最新，返回本次实际执行的迁移。"""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _lock_key(namespace))
        return await _apply_pending(conn, namespace)


async def current_version(pool: asyncpg.Pool, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> int:
    """数据库里的表结构版本；从未迁移过返回 0。"""
    ledger = namespace.table(_LEDGER)
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", ledger):
            return 0
        version = await conn.fetchval(f"SELECT max(version) FROM {ledger}")
    return int(version) if version is not None else 0


async def ensure_current(pool: asyncpg.Pool, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
    """核对表结构版本，落后则报错。只读。"""
    required = latest_version()
    current = await current_version(pool, namespace)
    if current < required:
        raise SchemaError(
            f"控制面表结构版本为 {current}，代码要求 {required}；请先运行 xuanpu migrate"
        )


__all__ = [
    "DEFAULT_NAMESPACE",
    "Migration",
    "SchemaError",
    "apply_migrations",
    "current_version",
    "ensure_current",
    "latest_version",
    "load_migrations",
]

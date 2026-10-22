"""独立非超级用户在真实两库之间双向连接拒绝的隔离演练。"""

from __future__ import annotations

import os
import re
import secrets
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest


async def test_independent_database_roles_cannot_cross_connect() -> None:
    """只在明确finance_test管理实例创建临时库和角色，任何断言失败均清理。"""
    dsn = os.environ.get("FINANCE_TEST_DSN")
    if not dsn:
        pytest.skip("须显式提供独立FINANCE_TEST_DSN")
    parts = urlsplit(dsn)
    assert parts.scheme in {"postgresql","postgres"} and parts.hostname in {"127.0.0.1", "localhost", "::1"}
    assert parts.username and parts.password and parts.port is not None
    assert re.fullmatch(r"/finance_test[a-zA-Z0-9_]*",parts.path) and not parts.query and not parts.fragment
    admin = await asyncpg.connect(dsn)
    suffix = uuid4().hex
    databases = ["finance_test_isolation_" + suffix, "finance_test_platform_" + suffix]
    roles = ["finance_role_" + suffix, "platform_role_" + suffix]
    passwords = [secrets.token_hex(24), secrets.token_hex(24)]
    made_databases = []
    made_roles = []
    try:
        assert (await admin.fetchval("SELECT current_database()")).startswith("finance_test")
        for role, password in zip(roles, passwords, strict=True):
            await admin.execute(
                f"CREATE ROLE \"{role}\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{password}'"
            )
            made_roles.append(role)
        for database, role in zip(databases, roles, strict=True):
            await admin.execute(f'CREATE DATABASE "{database}" OWNER "{role}"')
            made_databases.append(database)
            await admin.execute(f'REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC')
        for i in range(2):
            host="[::1]" if parts.hostname=="::1" else parts.hostname
            netloc = f"{roles[i]}:{passwords[i]}@{host}:{parts.port}"
            own = urlunsplit(
                parts._replace(netloc=netloc, path="/" + databases[i], query="", fragment="")
            )
            conn = await asyncpg.connect(own)
            try:
                assert await conn.fetchval("SELECT current_user") == roles[i]
                await conn.execute("CREATE TABLE isolated_original(id integer PRIMARY KEY)")
                await conn.execute("INSERT INTO isolated_original VALUES(1)")
                assert await conn.fetchval("SELECT count(*) FROM isolated_original") == 1
                assert not await conn.fetchval(
                    "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
                )
            finally:
                await conn.close()
            other = urlunsplit(
                parts._replace(netloc=netloc, path="/" + databases[1 - i], query="", fragment="")
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                unexpected = await asyncpg.connect(other)
                await unexpected.close()
    finally:
        for database in reversed(made_databases):
            await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
        for role in reversed(made_roles):
            await admin.execute(f'DROP ROLE "{role}"')
        remaining = await admin.fetchval(
            "SELECT count(*) FROM pg_database WHERE datname=ANY($1::text[])", databases
        )
        remaining_roles = await admin.fetchval(
            "SELECT count(*) FROM pg_roles WHERE rolname=ANY($1::text[])", roles
        )
        await admin.close()
        assert remaining == 0 and remaining_roles == 0


async def test_isolation_rejects_host_override_before_connection(monkeypatch) -> None:
    """直跑隔离用例也不能被查询参数改向其他数据库。"""
    monkeypatch.setenv('FINANCE_TEST_DSN','postgresql://u:p@127.0.0.1:5432/finance_test?host=remote.example')
    with pytest.raises(AssertionError):
        await test_independent_database_roles_cannot_cross_connect()

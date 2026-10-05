"""控制面表结构的升级：已有部署的库从旧版本迁到最新，旧数据保留、新列取默认值。只对 Postgres。"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import asyncpg
import pytest
from taifeng_stores import SqlNamespace
from xuanpu_server.controlplane import AuditQuery, AuditRecord, SessionRecord
from xuanpu_server.controlplane import schema as schema_module
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.controlplane.schema import (
    apply_migrations,
    current_version,
    ensure_current,
    latest_version,
    load_migrations,
)
from xuanpu_server.errors import SchemaError
from xuanpu_server.identity import UserQuery, UserRecord
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.tenancy import ApiKeyRecord

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def blank(postgres_dsn: str) -> AsyncIterator[tuple[asyncpg.Pool, SqlNamespace]]:
    """一个独占的空 schema；用完删掉。"""
    pool = await asyncpg.create_pool(postgres_dsn, min_size=1, max_size=4)
    namespace = SqlNamespace(schema="t_" + secrets.token_hex(6), prefix="xp_")
    try:
        yield pool, namespace
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace.schema}" CASCADE')
        await pool.close()


_SEED_VERSION_3 = (
    ("tenant", "(tenant_id, name, status, created_at) VALUES ('acme', 'Acme', 'active', 1.0)"),
    (
        "api_key",
        "(key_id, tenant_id, name, role, secret_hash, created_at) "
        "VALUES ('aaaaaaaaaaaa', 'acme', 'ci', 'tenant_admin', 'h1', 2.0)",
    ),
    (
        "session",
        "(tenant_id, session_id, package, version, entry, status, created_at, updated_at, "
        "created_by, metadata) VALUES ('acme', 's1', 'support', '1.0.0', 'triage', 'active', "
        """3.0, 4.0, 'key:aaaaaaaaaaaa', '{"customer_id": "C-42"}'::jsonb)""",
    ),
    (
        "audit",
        "(tenant_id, source, kind, at, actor, session_id, data) VALUES ('acme', 'control', "
        """'session_created', 3.0, 'key:aaaaaaaaaaaa', 's1', '{"package": "support"}'::jsonb)""",
    ),
)
"""按 0003 的表结构写的一份旧数据：租户、凭据、会话、审计（表名 → 列与取值）。"""


async def _seed_version_3(pool: asyncpg.Pool, namespace: SqlNamespace) -> None:
    """把 ``_SEED_VERSION_3`` 写进 0003 的表里。"""
    async with pool.acquire() as conn:
        for table, values in _SEED_VERSION_3:
            # 表名来自 SqlNamespace（字符集已校验），其余是上面写死的字面量
            await conn.execute(f"INSERT INTO {namespace.table(table)} {values}")  # noqa: S608


async def test_upgrade_from_version_3_keeps_data(
    blank: tuple[asyncpg.Pool, SqlNamespace], monkeypatch: pytest.MonkeyPatch
) -> None:
    """只迁到 0003 的库：落后于代码时报错；迁到最新后旧数据都在，新列为默认值，新表可用。"""
    pool, namespace = blank
    assert latest_version() == 7
    first_three = [m for m in load_migrations() if m.version <= 3]
    with monkeypatch.context() as patch:
        patch.setattr(schema_module, "load_migrations", lambda: first_three)
        executed = await apply_migrations(pool, namespace)
    assert [m.version for m in executed] == [1, 2, 3]
    await _seed_version_3(pool, namespace)

    with pytest.raises(SchemaError, match="版本为 3，代码要求 7"):
        await ensure_current(pool, namespace)

    upgraded = await apply_migrations(pool, namespace)
    assert [(m.version, m.name) for m in upgraded] == [
        (4, "identity"),
        (5, "identity_optional_event_secrets"),
        (6, "reminders"),
        (7, "workspace"),
    ]
    assert await current_version(pool, namespace) == 7
    assert await apply_migrations(pool, namespace) == []

    store = PostgresControlPlane(pool, namespace=namespace)
    await store.check_schema()
    assert await store.get_api_key("aaaaaaaaaaaa") == ApiKeyRecord(
        "aaaaaaaaaaaa", "acme", "ci", "tenant_admin", "h1", 2.0, can_act_as_user=False
    )
    assert await store.get_session("acme", "s1") == SessionRecord(
        session_id="s1",
        tenant_id="acme",
        package="support",
        version="1.0.0",
        entry="triage",
        created_at=3.0,
        updated_at=4.0,
        created_by="key:aaaaaaaaaaaa",
        metadata={"customer_id": "C-42"},
        user_id=None,
    )
    [audit] = await store.query_audit("acme", AuditQuery())
    assert audit == AuditRecord(
        tenant_id="acme",
        source="control",
        kind="session_created",
        at=3.0,
        actor="key:aaaaaaaaaaaa",
        session_id="s1",
        data={"package": "support"},
        user_id=None,
        audit_id=audit.audit_id,
    )

    identity = PostgresIdentityStore(pool, namespace=namespace)
    user = UserRecord(tenant_id="acme", user_id="usr_a", display_name="张三", created_at=5.0)
    await identity.create_user(user)
    assert await identity.list_users("acme", UserQuery()) == [user]

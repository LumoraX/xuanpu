"""控制面表结构的升级：已有部署的库从旧版本迁到最新，旧数据保留、新列取默认值。只对 Postgres。"""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING

import asyncpg
import pytest
from taifeng_stores import SqlNamespace

from server.core.controlplane import AuditQuery, AuditRecord, SessionRecord
from server.core.controlplane import schema as schema_module
from server.core.controlplane.postgres import PostgresControlPlane
from server.core.controlplane.schema import (
    apply_migrations,
    current_version,
    ensure_current,
    latest_version,
    load_migrations,
)
from server.core.errors import SchemaError
from server.core.tenancy import ApiKeyRecord
from server.domains.identity import UserQuery, UserRecord
from server.domains.identity.postgres import PostgresIdentityStore

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
    assert latest_version() == 33
    first_three = [m for m in load_migrations() if m.version <= 3]
    with monkeypatch.context() as patch:
        patch.setattr(schema_module, "load_migrations", lambda: first_three)
        executed = await apply_migrations(pool, namespace)
    assert [m.version for m in executed] == [1, 2, 3]
    await _seed_version_3(pool, namespace)

    with pytest.raises(SchemaError, match="版本为 3，代码要求 33"):
        await ensure_current(pool, namespace)

    upgraded = await apply_migrations(pool, namespace)
    assert [(m.version, m.name) for m in upgraded] == [
        (4, "identity"),
        (5, "identity_optional_event_secrets"),
        (6, "reminders"),
        (7, "workspace"),
        (8, "proactive"),
        (9, "operation_receipts"),
        (10, "work_tasks"),
        (11, "task_execution"),
        (12, "task_effects"),
        (13, "task_triggers"),
        (14, "task_activity"),
        (15, "connection_identity"),
        (16, "organization_authority"),
        (17, "shared_matters"),
        (18, "handoffs"),
        (19, "organizational_approvals"),
        (20, "capability_delivery"),
        (21, "execution_spans"),
        (22, "butler_dispatch"),
        (23, "butler_wakeup"),
        (24, "artisan_groups"),
        (25, "group_messages"),
        (26, "local_credential"),
        (27, "desktop_handoff"),
        (28, "capability_assignments"),
        (29, "capability_releases"),
        (30, "knowledge_governance"),
        (31, "business_outcomes"),
        (32, "knowledge_contexts"),
        (33, "personal_people"),
    ]
    assert await current_version(pool, namespace) == 33
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
    async with store.connection_store.transaction("acme") as state:
        state.records["migration-probe"]={"kind":"probe"}
    async with store.connection_store.transaction("acme") as state:
        assert state.records["migration-probe"]=={"kind":"probe"}


async def test_upgrade_eight_to_nine_keeps_existing_session(blank: tuple[asyncpg.Pool, SqlNamespace], monkeypatch: pytest.MonkeyPatch) -> None:
    """从0008升级保留R0回执并接入R1任务，二次迁移无操作。"""
    pool, namespace = blank
    migrations = [item for item in load_migrations() if item.version <= 8]
    with monkeypatch.context() as patch:
        patch.setattr(schema_module, "load_migrations", lambda: migrations)
        await apply_migrations(pool, namespace)
    await _seed_version_3(pool, namespace)
    store = PostgresControlPlane(pool, namespace=namespace)
    async with pool.acquire() as conn:
        row = dict(await conn.fetchrow(f"SELECT * FROM {namespace.table('session')} WHERE tenant_id='acme' AND session_id='s1'"))
    row["metadata"] = json.loads(row["metadata"])
    before = SessionRecord(**row)
    assert [item.version for item in await apply_migrations(pool, namespace)] == [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33]
    assert await store.get_session("acme", "s1") == before
    assert await apply_migrations(pool, namespace) == []
    receipt = await store.operation_receipts.claim("acme", "s1", {"r1": True}, at=5)
    assert receipt.state == "claimed"


async def test_desktop_handoff_deployed_as_0026_reconciles_to_0027(
    blank: tuple[asyncpg.Pool, SqlNamespace], monkeypatch: pytest.MonkeyPatch
) -> None:
    """桌面接力最初以 0026_desktop_handoff 部署（与主线 0026_local_credential 撞号）：直接升级会因
    0026 摘要不符拒绝；删掉台账里那一行后再迁移，补上 0026_local_credential，0027 重跑不出错，
    已有的接力表结构与数据保留（ADR 0021，运维手册的一次性对账）。"""
    pool, namespace = blank
    migrations = load_migrations()
    handoff = next(item for item in migrations if item.name == "desktop_handoff")
    deployed = [item for item in migrations if item.version <= 25]
    deployed.append(schema_module.Migration(26, "desktop_handoff", handoff.sql_template))
    with monkeypatch.context() as patch:
        patch.setattr(schema_module, "load_migrations", lambda: deployed)
        await apply_migrations(pool, namespace)
    attempts = namespace.table("login_attempt")
    async with pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO {namespace.table('tenant')} (tenant_id, name, created_at, status) "  # noqa: S608
            "VALUES ('acme', 'Acme', 1, 'active')"
        )
        await conn.execute(
            f"INSERT INTO {attempts} (state, tenant_id, code_verifier, return_to, created_at, "  # noqa: S608
            "expires_at, cookie_hash, client) VALUES ('s1', 'acme', 'v', '/', 1, 2, 'c', 'desktop')"
        )

    with pytest.raises(SchemaError, match="0026"):
        await apply_migrations(pool, namespace)

    ledger = namespace.table("schema_migrations")
    async with pool.acquire() as conn:
        await conn.execute(
            f"DELETE FROM {ledger} WHERE version = 26 AND name = 'desktop_handoff'"  # noqa: S608
        )
    executed = await apply_migrations(pool, namespace)
    assert [(item.version, item.name) for item in executed] == [
        (26, "local_credential"),
        (27, "desktop_handoff"),
        (28, "capability_assignments"),
        (29, "capability_releases"),
        (30, "knowledge_governance"),
        (31, "business_outcomes"),
        (32, "knowledge_contexts"),
        (33, "personal_people"),
    ]
    await ensure_current(pool, namespace)
    async with pool.acquire() as conn:
        assert await conn.fetchval(f"SELECT client FROM {attempts} WHERE state = 's1'") == "desktop"  # noqa: S608

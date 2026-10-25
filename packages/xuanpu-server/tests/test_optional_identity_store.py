"""可空事件密钥引用的内存/Postgres往返与旧库升级契约。"""

from __future__ import annotations

import secrets
from dataclasses import replace
from typing import TYPE_CHECKING

import asyncpg
import pytest
from taifeng_stores import SqlNamespace
from xuanpu_server.controlplane import schema as schema_module
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.controlplane.schema import apply_migrations, current_version, load_migrations
from xuanpu_server.identity import IdentityStore, TenantIdentityConfig
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.tenancy import Tenant

if TYPE_CHECKING:
    from xuanpu_server.controlplane import ControlPlaneStore


def test_event_refs_default_to_none_and_empty_normalizes() -> None:
    """领域配置允许省略事件引用，空字符串与 None 均代表关闭。"""
    config = TenantIdentityConfig("acme", "feishu", "app", "secret")
    assert config.encrypt_key_ref is config.verification_token_ref is None
    assert config.events_enabled is False
    assert replace(config, encrypt_key_ref="", verification_token_ref="") == config


@pytest.mark.parametrize("refs", [(None, None), (None, "verify"), ("encrypt", None), ("encrypt", "verify")])
async def test_nullable_event_refs_roundtrip(
    identity_stores: tuple[ControlPlaneStore, IdentityStore], refs: tuple[str | None, str | None]
) -> None:
    """两个实现保留可空字段，更新配置不丢失已绑定的飞书企业且不跨租户。"""
    control, store = identity_stores
    await control.create_tenant(Tenant(tenant_id="acme", name="Acme", created_at=1.0))
    config = TenantIdentityConfig("acme", "feishu", "app", "secret", refs[0], refs[1])
    await store.put_identity_config(config)
    assert await store.get_identity_config("acme", "feishu") == config
    assert await store.list_identity_configs("feishu") == [config]
    assert await store.get_identity_config("other", "feishu") is None
    await store.bind_tenant_key("acme", "feishu", "enterprise")
    changed = replace(config, encrypt_key_ref=None, verification_token_ref=None)
    await store.put_identity_config(changed)
    assert await store.get_identity_config("acme", "feishu") == replace(changed, feishu_tenant_key="enterprise")


async def test_version_four_upgrades_without_rewriting_or_losing_refs(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0004 已有配置原样保留；仅新迁移 0005 放开可空事件列，重复升级无操作。"""
    migrations = load_migrations()
    assert migrations[-1].version == 20
    old = [migration for migration in migrations if migration.version <= 4]
    pool = await asyncpg.create_pool(postgres_dsn, min_size=1, max_size=2)
    namespace = SqlNamespace(schema="t_" + secrets.token_hex(6), prefix="xp_")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(schema_module, "load_migrations", lambda: old)
            await apply_migrations(pool, namespace)
        control = PostgresControlPlane(pool, namespace=namespace)
        store = PostgresIdentityStore(pool, namespace=namespace)
        await control.create_tenant(Tenant(tenant_id="acme", name="Acme", created_at=1.0))
        original = TenantIdentityConfig("acme", "feishu", "app", "secret", "encrypt", "verify")
        await store.put_identity_config(original)
        await store.bind_tenant_key("acme", "feishu", "enterprise")
        executed = await apply_migrations(pool, namespace)
        assert [migration.version for migration in executed] == [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
        assert await current_version(pool, namespace) == 20
        assert await apply_migrations(pool, namespace) == []
        assert await store.get_identity_config("acme", "feishu") == replace(original, feishu_tenant_key="enterprise")
        await store.put_identity_config(replace(original, encrypt_key_ref=None, verification_token_ref=None))
        nullable = await store.get_identity_config("acme", "feishu")
        assert nullable is not None
        assert nullable.encrypt_key_ref is nullable.verification_token_ref is None
        assert nullable.feishu_tenant_key == "enterprise"
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace.schema}" CASCADE')
        await pool.close()

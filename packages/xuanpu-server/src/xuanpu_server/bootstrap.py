"""按配置装配平台。"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.objectstore import LocalObjectStore, S3ObjectStore, S3Settings
from xuanpu_server.platform import Platform
from xuanpu_server.runtime import (
    DockerSandboxProvider,
    GatewayConfig,
    GatewayModelClientFactory,
    LocalSandboxProvider,
    LocalStateBackends,
    ModelProfile,
    RuntimeDefaults,
    StateBackends,
)
from xuanpu_server.secrets import EncryptedVault

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg

    from xuanpu_server.config import PostgresConfig, ServerConfig
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.objectstore import ObjectStore
    from xuanpu_server.runtime import SandboxProvider, StateBackendFactory


def tenant_table_prefix(tenant_id: str) -> str:
    """租户在状态库里的表名前缀。

    租户标识里可以有连字符，表名里不行；用摘要既满足字符集，又不会因为截断而撞名。
    """
    return "t" + hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12] + "_"


class PostgresStateBackends:
    """把线程目录与消息镜像放进 Postgres，每个租户一组带专属前缀的表。"""

    def __init__(self, pool: asyncpg.Pool, *, schema: str) -> None:
        """
        Args:
            pool: 连接池。
            schema: 表所在的 schema。
        """
        self._pool = pool
        self._schema = schema
        self._ready: dict[str, StateBackends] = {}
        self._lock = asyncio.Lock()

    async def for_tenant(self, tenant_id: str) -> StateBackends:
        """返回租户的后端；首次使用时为它建表（开通租户的一部分，不是运行时的隐式行为）。"""
        from taifeng_stores import SqlNamespace
        from taifeng_stores.postgres import (
            PostgresMessageMirror,
            PostgresThreadDirectory,
            apply_migrations,
        )

        async with self._lock:
            ready = self._ready.get(tenant_id)
            if ready is None:
                namespace = SqlNamespace(schema=self._schema, prefix=tenant_table_prefix(tenant_id))
                await apply_migrations(self._pool, namespace)
                ready = StateBackends(
                    thread_directory=PostgresThreadDirectory(self._pool, namespace=namespace),
                    index_hook=PostgresMessageMirror(self._pool, namespace=namespace),
                )
                self._ready[tenant_id] = ready
            return ready


async def _open_pool(stack: AsyncExitStack, config: PostgresConfig) -> asyncpg.Pool:
    """建连接池，并登记到退出栈里。"""
    import asyncpg

    pool = await asyncpg.create_pool(config.dsn, min_size=config.pool_min, max_size=config.pool_max)
    stack.push_async_callback(pool.close)
    return pool


async def _control_plane(stack: AsyncExitStack, config: ServerConfig) -> ControlPlaneStore:
    """控制面存储。表结构落后时在这里就报错，而不是等到第一个请求。"""
    if config.control_plane.backend == "memory":
        return InMemoryControlPlane()
    from taifeng_stores import SqlNamespace

    from xuanpu_server.controlplane.postgres import PostgresControlPlane

    assert config.control_plane.postgres is not None
    pool = await _open_pool(stack, config.control_plane.postgres)
    store = PostgresControlPlane(
        pool,
        namespace=SqlNamespace(schema=config.control_plane.postgres.schema_name, prefix="xp_"),
    )
    await store.check_schema()
    return store


async def _states(stack: AsyncExitStack, config: ServerConfig) -> StateBackendFactory:
    """状态面后端。"""
    if config.state.backend == "local":
        return LocalStateBackends()
    assert config.state.postgres is not None
    pool = await _open_pool(stack, config.state.postgres)
    return PostgresStateBackends(pool, schema=config.state.postgres.schema_name)


def _sandboxes(config: ServerConfig) -> SandboxProvider:
    """沙盒供给方。"""
    if config.sandbox.backend == "local":
        return LocalSandboxProvider()
    return DockerSandboxProvider(
        image=config.sandbox.image,
        docker_env=config.sandbox.docker_env,
        memory_mb=config.sandbox.memory_mb,
        cpus=config.sandbox.cpus,
        docker_binary=config.sandbox.docker_binary,
    )


def _object_store(config: ServerConfig) -> ObjectStore:
    """对象存储。``local`` 的目录缺省为 ``<data_dir>/objects``；``s3`` 构造时不发请求。"""
    section = config.object_store
    if section.backend == "local":
        return LocalObjectStore(section.local.root or config.data_dir / "objects")
    assert section.s3 is not None
    return S3ObjectStore(
        S3Settings(
            endpoint=section.s3.endpoint,
            bucket=section.s3.bucket,
            region=section.s3.region,
            access_key_id=section.s3.access_key_id,
            secret_access_key=section.s3.secret_access_key,
            prefix=section.s3.prefix,
            addressing_style=section.s3.addressing_style,
        )
    )


@asynccontextmanager
async def open_platform(config: ServerConfig) -> AsyncIterator[Platform]:
    """按配置装配平台；退出时关闭平台并释放连接池。"""
    async with AsyncExitStack() as stack:
        await asyncio.to_thread(config.data_dir.mkdir, parents=True, exist_ok=True)
        store = await _control_plane(stack, config)
        vault = EncryptedVault(
            store, keys=config.decoded_master_keys(), primary=config.primary_master_key
        )
        gateway = GatewayConfig(
            base_url=config.models.base_url,
            api_key=config.models.api_key,
            api_key_secret=config.models.api_key_secret,
            profiles={
                tier: ModelProfile(model=item.model, timeout_seconds=item.timeout_seconds)
                for tier, item in config.models.tiers.items()
            },
        )
        platform = Platform(
            store=store,
            vault=vault,
            models=GatewayModelClientFactory(gateway, vault),
            sandboxes=_sandboxes(config),
            states=await _states(stack, config),
            data_dir=config.data_dir,
            root_key_hash=config.root_key_hash,
            defaults=RuntimeDefaults(
                max_session_tokens=config.limits.max_session_tokens,
                max_iterations=config.limits.max_iterations,
                connector_timeout_seconds=config.limits.connector_timeout_seconds,
                capture_requests=config.audit.capture_requests,
                session_idle_seconds=config.limits.session_idle_seconds,
                runtime_idle_seconds=config.limits.runtime_idle_seconds,
            ),
            instance_id=config.instance_id,
            lease_seconds=config.lease_seconds,
            shutdown_drain_seconds=config.shutdown_drain_seconds,
        )
        stack.push_async_callback(platform.close)
        yield platform


__all__ = ["PostgresStateBackends", "open_platform", "tenant_table_prefix"]

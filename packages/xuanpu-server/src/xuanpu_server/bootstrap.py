"""按配置装配平台。"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from xuanpu_server.config import ConfigError
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.identity import InMemoryIdentityStore
from xuanpu_server.identity.service import IdentitySettings
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
    from xuanpu_server.identity import IdentityStore
    from xuanpu_server.objectstore import ObjectStore
    from xuanpu_server.runtime import SandboxProvider, StateBackendFactory


def tenant_table_prefix(tenant_id: str) -> str:
    """租户在状态库里的表名前缀。

    租户标识里可以有连字符，表名里不行；用摘要既满足字符集，又不会因为截断而撞名。
    """
    return "t" + hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12] + "_"


class PostgresStateBackends:
    """把线程目录与会话主存放进 Postgres，每个租户一组带专属前缀的表。

    会话主存在库里，各实例不需要为接手会话共享数据目录。每个运行时（引擎池）用一个
    自己的 ``PostgresMessageStore``，全部共用同一个连接池；引擎池关闭时会调用存储的
    ``close()``，那不会关闭连接池（连接池归 ``open_platform`` 管）。

    不挂 ``PostgresMessageMirror``：它与主存写的是同一张消息表。早先版本把消息镜像进库，
    镜像是尽力写入，可能不完整；升级前若有活动会话，以节点本地的 JSONL 为准，升级后这些
    会话可能无法续接。单写者由会话租约保证（见 ``SessionManager``）。
    """

    def __init__(self, pool: asyncpg.Pool, *, schema: str) -> None:
        """
        Args:
            pool: 连接池。
            schema: 表所在的 schema。
        """
        self._pool = pool
        self._schema = schema
        self._ready: dict[str, StateBackends] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def for_tenant(self, tenant_id: str) -> StateBackends:
        """返回租户的后端；首次使用时为它建表（开通租户的一部分，不是运行时的隐式行为）。

        每个租户一把锁：同一租户的并发首次使用只建一次表，某个租户建表慢不拖住别的租户。
        """
        ready = self._ready.get(tenant_id)
        if ready is not None:
            return ready
        async with self._locks.setdefault(tenant_id, asyncio.Lock()):
            ready = self._ready.get(tenant_id)
            if ready is None:
                ready = await self._prepare(tenant_id)
                self._ready[tenant_id] = ready
            return ready

    async def _prepare(self, tenant_id: str) -> StateBackends:
        """为租户建表（已建过的迁移会跳过），返回它的线程目录与会话主存工厂。"""
        from taifeng_stores import SqlNamespace
        from taifeng_stores.postgres import (
            PostgresMessageStore,
            PostgresThreadDirectory,
            apply_migrations,
        )

        namespace = SqlNamespace(schema=self._schema, prefix=tenant_table_prefix(tenant_id))
        await apply_migrations(self._pool, namespace)
        pool = self._pool

        def message_store() -> PostgresMessageStore:
            """给一个运行时造它专用的会话主存（与线程目录同库、同命名空间）。"""
            return PostgresMessageStore(pool, namespace=namespace)

        return StateBackends(
            thread_directory=PostgresThreadDirectory(pool, namespace=namespace),
            message_store=message_store,
        )


async def _open_pool(stack: AsyncExitStack, config: PostgresConfig) -> asyncpg.Pool:
    """建连接池，并登记到退出栈里。"""
    import asyncpg

    pool = await asyncpg.create_pool(
        config.dsn,
        min_size=config.pool_min,
        max_size=config.pool_max,
        # 新建连接与单条语句都有时限：库卡住时报错，而不是让请求和续期一直挂着
        timeout=config.connect_timeout_seconds,
        command_timeout=config.command_timeout_seconds,
    )
    stack.push_async_callback(pool.close)
    return pool


async def _stores(
    stack: AsyncExitStack, config: ServerConfig
) -> tuple[ControlPlaneStore, IdentityStore]:
    """控制面存储与身份存储，两者同一后端。表结构落后时在这里就报错，而不是等到第一个请求。

    ``memory``：身份存储以内存控制面为准判断租户是否存在。``postgres``：身份存储与控制面共用
    同一个连接池与命名空间，表由控制面的迁移建出（``0004_identity``）。
    """
    if config.control_plane.backend == "memory":
        control = InMemoryControlPlane()
        return control, InMemoryIdentityStore(control)
    from taifeng_stores import SqlNamespace

    from xuanpu_server.controlplane.postgres import PostgresControlPlane
    from xuanpu_server.identity.postgres import PostgresIdentityStore

    assert config.control_plane.postgres is not None
    pool = await _open_pool(stack, config.control_plane.postgres)
    namespace = SqlNamespace(schema=config.control_plane.postgres.schema_name, prefix="xp_")
    store = PostgresControlPlane(pool, namespace=namespace)
    await store.check_schema()
    return store, PostgresIdentityStore(pool, namespace=namespace)


async def _states(stack: AsyncExitStack, config: ServerConfig) -> StateBackendFactory:
    """状态面后端。"""
    if config.state.backend == "local":
        return LocalStateBackends()
    assert config.state.postgres is not None
    pool = await _open_pool(stack, config.state.postgres)
    return PostgresStateBackends(pool, schema=config.state.postgres.schema_name)


def _connector_readable_roots(config: ServerConfig) -> tuple[Path, ...]:
    """本机连接器沙盒额外可读的目录：先解析成真实路径，再逐个校验。

    放开它们等于让每个租户的连接器都能读，所以拒绝根目录，也拒绝与数据目录、本机对象
    存储目录相同或互为上下级的目录（那里有别的租户的数据）。解析符号链接之后再比，免得
    借链接绕过。目录必须已经存在：不存在多半是配错了，连接器会起不来。

    Raises:
        ConfigError: 有目录不合要求。
    """
    guarded = {"data_dir": config.data_dir.resolve()}
    store = config.object_store
    if store.backend == "local" and store.local.root is not None:
        guarded["object_store.local.root"] = store.local.root.resolve()
    roots: list[Path] = []
    for root in config.sandbox.connector_readable_roots:
        resolved = root.resolve()
        where = f"sandbox.connector_readable_roots 里的 {root}（解析为 {resolved}）"
        if resolved == Path(resolved.anchor):
            raise ConfigError(f"{where} 是根目录：等于让连接器读整个文件系统")
        for name, path in guarded.items():
            if resolved == path or resolved in path.parents or path in resolved.parents:
                raise ConfigError(
                    f"{where} 与 {name}（{path}）相同或互为上下级：每个租户的连接器都能读到"
                    "那里的平台数据，包括别的租户的"
                )
        if not resolved.is_dir():
            raise ConfigError(f"{where} 不存在或不是目录")
        roots.append(resolved)
    return tuple(roots)


def _sandboxes(config: ServerConfig) -> SandboxProvider:
    """沙盒供给方（脚本沙盒与连接器沙盒都由它开）。

    Raises:
        ConfigError: 连接器沙盒的可读目录不合要求（见 ``_connector_readable_roots``）。
    """
    section = config.sandbox
    if section.backend == "local":
        return LocalSandboxProvider(connector_readable_roots=_connector_readable_roots(config))
    return DockerSandboxProvider(
        image=section.image,
        docker_env=section.docker_env,
        memory_mb=section.memory_mb,
        cpus=section.cpus,
        docker_binary=section.docker_binary,
        connector_image=section.connector_image,
    )


def object_store(config: ServerConfig) -> ObjectStore:
    """按配置建对象存储。``local`` 的目录缺省为 ``<data_dir>/objects``；``s3`` 构造时不发请求。"""
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
        # 沙盒配置先查：配错了在连库之前就报出来
        sandboxes = await asyncio.to_thread(_sandboxes, config)
        store, identity_store = await _stores(stack, config)
        vault = EncryptedVault(
            store, keys=config.decoded_master_keys(), primary=config.primary_master_key
        )
        gateway = GatewayConfig(
            base_url=config.models.base_url,
            protocol=config.models.protocol,
            api_key=config.models.api_key,
            api_key_secret=config.models.api_key_secret,
            profiles={
                tier: ModelProfile(model=item.model, timeout_seconds=item.timeout_seconds)
                for tier, item in config.models.tiers.items()
            },
        )
        platform = Platform(
            store=store,
            identity_store=identity_store,
            identity_settings=IdentitySettings.from_config(config.public_url, config.identity),
            vault=vault,
            models=GatewayModelClientFactory(gateway, vault),
            sandboxes=sandboxes,
            states=await _states(stack, config),
            data_dir=config.data_dir,
            root_key_hash=config.root_key_hash,
            metrics_token_hash=config.metrics.token_hash,
            objects=object_store(config),
            defaults=RuntimeDefaults(
                max_session_tokens=config.limits.max_session_tokens,
                max_iterations=config.limits.max_iterations,
                connector_timeout_seconds=config.limits.connector_timeout_seconds,
                connector_startup_seconds=config.limits.connector_startup_seconds,
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


__all__ = ["PostgresStateBackends", "object_store", "open_platform", "tenant_table_prefix"]

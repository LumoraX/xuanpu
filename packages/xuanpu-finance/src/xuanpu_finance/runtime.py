"""独立连接池、对象存储、网关与单实例财务 worker 的资源生命周期。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

import asyncpg
import httpx

from xuanpu_finance.api import create_app
from xuanpu_finance.auth import FinanceAuthenticator
from xuanpu_finance.compliance.archive import ArchivePipeline
from xuanpu_finance.compliance.evidence import EvidenceFetcher
from xuanpu_finance.compliance.provider import ComplianceRegistry
from xuanpu_finance.compliance.queue import ComplianceQueueAdapter
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.gateway import PlatformGateway
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.notifications import NotificationsWorker
from xuanpu_finance.objects import EncryptedLocalObjects, EncryptedS3Objects
from xuanpu_finance.settlement.providers import ProviderRegistry
from xuanpu_finance.settlement.worker import SettlementWorker
from xuanpu_finance.startup import prepare_startup
from xuanpu_finance.storage import FinanceStore
from xuanpu_finance.storage.f9_compatibility import check_internal_database

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

    from xuanpu_finance.config import FinanceConfig, ObjectsConfig
    from xuanpu_finance.objects import OriginalObjects
    from xuanpu_finance.readiness.credentials import CredentialSnapshot

WORKER_LOCK = 617472002


async def secret_text(path: Path) -> str:
    """只在配置指定的固定路径读凭据，不打印或写入业务记录。"""
    text = (await asyncio.to_thread(path.read_text, encoding="utf-8")).strip()
    if not text:
        raise ValueError("财务服务密钥文件不能为空")
    return text


async def objects_from_config(
    config: ObjectsConfig, *, simulation: bool = False,
    credentials: CredentialSnapshot | None = None,
) -> OriginalObjects:
    """只创建财务自身对象适配器，不取得平台存储凭据。"""
    key = (credentials.aes_key if credentials
           else await asyncio.to_thread(config.key_file.read_bytes))
    if config.kind == "local":
        assert config.root is not None
        return EncryptedLocalObjects(config.root / "simulation" if simulation else config.root, key)
    import boto3

    assert config.access_key_file is not None and config.secret_key_file is not None
    assert config.bucket is not None
    client = await asyncio.to_thread(
        boto3.client,
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=(credentials.access_key if credentials
                           else await secret_text(config.access_key_file)),
        aws_secret_access_key=(credentials.secret_key if credentials
                               else await secret_text(config.secret_key_file)),
        region_name="us-east-1",
    )
    return EncryptedS3Objects(
        client, config.bucket, key, prefix="finance-simulation" if simulation else "finance"
    )


async def configure_store(pool: asyncpg.Pool, config: FinanceConfig) -> FinanceStore:
    """先核对模拟库真实身份，再迁移和注入独立公司/业务配置。"""
    if config.profile == "internal_records":
        await check_internal_database(pool)
    if config.execution_simulation:
        database = await pool.fetchval("SELECT current_database()")
        if not isinstance(database, str) or not database.startswith("finance_test"):
            raise FinanceError(
                "SIMULATION_DATABASE_REQUIRED", 422, "模拟执行必须使用独立finance_test数据库"
            )
    store = FinanceStore(pool)
    await store.migrate()
    await store.configure(
        [item.model_dump() for item in config.companies],
        [item.model_dump() for item in config.grants],
    )
    await store.configure_expenses(config.expense_policies)
    await store.configure_settlements(
        config.settlement_policies,
        config.business_documents,
        config.payee_accounts,
        [item.public_descriptor() for item in config.settlement_providers],
        simulation=config.execution_simulation,
    )
    await store.configure_funding(config.funding_policies)
    await store.configure_compliance(config.compliance_policies,
        [item.public_descriptor() for item in config.compliance_providers],
        simulation=config.execution_simulation)
    return store


async def provider_registry(config: FinanceConfig, resources: AsyncExitStack) -> ProviderRegistry:
    """只读取固定凭据文件，专用HTTP客户端由资源栈可靠关闭。"""
    client = await resources.enter_async_context(
        httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False)
    )
    tokens, secrets = {}, {}
    for item in config.settlement_providers:
        selector = (item.provider, item.configuration_version)
        tokens[selector] = await secret_text(item.token_file)
        secrets[selector] = await secret_text(item.callback_secret_file)
        if len(secrets[selector].encode("utf-8")) < 32:
            raise ValueError("供应商回调密钥至少32字节")
    return ProviderRegistry(
        config.settlement_providers, client, tokens=tokens, callback_secrets=secrets
    )


@asynccontextmanager
async def open_runtime(config: FinanceConfig) -> AsyncIterator[FastAPI]:
    """持有独立数据库锁，先停止服务后台再关闭其资源。"""
    config, credentials = await prepare_startup(config)
    async with AsyncExitStack() as resources:
        dsn = credentials.dsn if credentials else await secret_text(config.database_dsn_file)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
        assert pool is not None
        resources.push_async_callback(pool.close)
        lock_dsn = dsn if credentials else await secret_text(config.database_dsn_file)
        lock = await asyncpg.connect(lock_dsn)
        resources.push_async_callback(lock.close)
        if not await lock.fetchval("SELECT pg_try_advisory_lock($1::bigint)", WORKER_LOCK):
            raise FinanceError("instance_owned", 409, "已有财务服务持有此独立数据库")
        store = await configure_store(pool, config)
        client = await resources.enter_async_context(
            httpx.AsyncClient(
                base_url=config.gateway.base_url,
                timeout=30,
                trust_env=False,
                follow_redirects=False,
            )
        )
        tokens = dict(credentials.gateway_tokens) if credentials else {
            tenant: await secret_text(path)
            for tenant, path in config.gateway.tenant_token_files.items()
        }
        gateway = PlatformGateway(client, tenant_tokens=tokens)
        keys = dict(credentials.public_keys) if credentials else {
            kid: await secret_text(path) for kid, path in config.public_key_files.items()
        }
        auth = FinanceAuthenticator(
            service_token=(credentials.service_token if credentials
                           else await secret_text(config.service_token_file)),
            issuer=config.issuer,
            audience=config.audience,
            public_keys=keys,
            store=store,
            gateway=gateway,
        )
        objects = await objects_from_config(
            config.objects, simulation=config.execution_simulation, credentials=credentials
        )
        if isinstance(objects, EncryptedS3Objects):
            resources.push_async_callback(asyncio.to_thread, objects.client.close)
        worker = FinanceWorker(store, objects, gateway, synthetic=config.synthetic)
        app = await runtime_app(config, resources, store, auth, worker, gateway, objects)
        app.state.finance_store, app.state.finance_objects = store, objects
        app.state.lock_pid = await lock.fetchval("SELECT pg_backend_pid()")

        def lock_lost(connection: object) -> None:
            """持锁连接丢失立即停止认领，并对外失败关闭。"""
            app.state.worker_error = "InstanceLockLost"
            app.state.instance_lock_lost = True
            app.state.mode = "disabled"
            for name in ("worker_task", "settlement_task", "compliance_task", "notification_task"):
                task = getattr(app.state, name, None)
                if task is not None:
                    task.cancel()

        lock.add_termination_listener(lock_lost)
        try:
            yield app
        finally:
            if not lock.is_closed():
                lock.remove_termination_listener(lock_lost)


async def compliance_registry(config: FinanceConfig, resources: AsyncExitStack) -> ComplianceRegistry:
    """合规通道使用自己的固定凭据和生命周期，不混用结算回调密钥。"""
    client = await resources.enter_async_context(httpx.AsyncClient(timeout=30, trust_env=False,
                                                                 follow_redirects=False))
    tokens, secrets = {}, {}
    for item in config.compliance_providers:
        selector = (item.provider, item.configuration_version)
        tokens[selector] = await secret_text(item.token_file)
        secrets[selector] = await secret_text(item.callback_secret_file)
        if len(secrets[selector].encode("utf-8")) < 32:
            raise ValueError("合规回调密钥至少32字节")
    return ComplianceRegistry(list(config.compliance_providers), client,
                              tokens=tokens, callback_secrets=secrets)


async def runtime_app(
    config: FinanceConfig, resources: AsyncExitStack, store: FinanceStore,
    auth: FinanceAuthenticator, worker: FinanceWorker, gateway: PlatformGateway,
    objects: OriginalObjects,
) -> FastAPI:
    """内部实例仅装配收件和本地通知，外部注册表与专用客户端都不创建。"""
    if config.profile == "internal_records":
        return create_app(store=store, auth=auth, worker=worker, mode=config.mode,
                          profile=config.profile, notification_worker=NotificationsWorker(store),
                          objects=objects)
    providers = await provider_registry(config, resources)
    settlement = SettlementWorker(store, providers, gateway)
    compliance = await compliance_registry(config, resources)
    archive = ArchivePipeline(compliance, objects, EvidenceFetcher({
        "settlement": providers, "compliance": compliance}))
    compliance.archive = archive
    compliance_worker = SettlementWorker(ComplianceQueueAdapter(store, archive), compliance, gateway)
    return create_app(store=store, auth=auth, worker=worker, mode=config.mode,
        profile=config.profile, settlement_worker=settlement, settlement_providers=providers,
        compliance_worker=compliance_worker, compliance_providers=compliance,
        notification_worker=NotificationsWorker(store), objects=objects)

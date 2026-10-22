"""只在空白本机测试库装配真实HTTP财务与浏览器入口，禁止生产身份和模型调用。"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import shutil
import signal
import socket
import stat
import sys
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from typing import Literal, Never

    from fastapi import FastAPI
    from taifeng import CancellationToken
    from xuanpu_finance.config import FinanceConfig
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def inputs(dsn_file: Path, ready_file: Path) -> str:
    """在创建身份、目录或监听器之前验证显式私有测试库输入。"""
    from scripts.acceptance.finance_runner import validate_dsn

    info = dsn_file.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 65536 or ready_file.exists() or ready_file.is_symlink()
            or not ready_file.parent.is_dir()):
        raise ValueError('BROWSER_ACCEPTANCE_INPUT_INVALID')
    dsn = dsn_file.read_text().strip()
    validate_dsn(dsn)
    return dsn


def free_port() -> int:
    """为本机隔离服务取得可用端口。"""
    with socket.socket() as channel:
        channel.bind(('127.0.0.1', 0))
        return int(channel.getsockname()[1])


@asynccontextmanager
async def listen(app: FastAPI, port: int) -> AsyncIterator[None]:
    """仅监听loopback并可靠关闭本入口创建的ASGI服务任务。"""
    import uvicorn

    class HarnessServer(uvicorn.Server):
        """由外层运行器统一接收信号，避免双服务互相覆盖处理器。"""

        @contextmanager
        def capture_signals(self) -> Iterator[None]:
            """保留外层已注册的SIGTERM/SIGINT停止事件。"""
            yield

    server = HarnessServer(uvicorn.Config(
        app, host='127.0.0.1', port=port, log_level='critical', access_log=False,
        timeout_graceful_shutdown=2,
    ))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError('LISTENER_FAILED')
                await asyncio.sleep(.02)
        yield
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)


class ModelBlocker:
    """同时遵循模型工厂和客户端协议，所有入口拒绝且记录实际拦截数。"""

    def __init__(self) -> None:
        """不存在底层模型或成功路径，已派发请求账本因此始终为空。"""
        self.requests: list[object] = []
        self.blocked_entries = {'runtime': 0, 'factory': 0, 'session': 0}

    @property
    def attempts(self) -> int:
        """返回所有入口实际收到并拒绝的请求数，不保存请求正文。"""
        return sum(self.blocked_entries.values())

    @property
    def blocked_attempts(self) -> int:
        """本工厂所有尝试均被拒绝，显式与已派发调用数区分。"""
        return self.attempts

    def _reject(self, entry: str) -> Never:
        """只记录入口分类，不保存员工、正文、模型参数或凭据。"""
        self.blocked_entries[entry] += 1
        raise RuntimeError('BROWSER_ACCEPTANCE_MODEL_DISABLED')

    async def __call__(self, *args: object, **kwargs: object) -> Never:
        """提前拒绝人工流程误触发的运行时装配。"""
        self._reject('runtime')

    async def create(self, tenant_id: str, tier: str) -> Never:
        """工厂入口直接失败，后台主动采样不能取得脚本或外部模型客户端。"""
        self._reject('factory')

    def session(self, *, cancel: CancellationToken, model: str | None = None) -> Never:
        """直接客户端入口也失败，不能创建会话或迭代模型输出流。"""
        self._reject('session')


async def ensure_empty_database(dsn: str) -> None:
    """只读验证实际库身份和所有用户schema，空白库以外不创建资源。"""
    import asyncpg

    from scripts.acceptance.finance_runner import check_database

    await check_database(dsn)
    conn = await asyncpg.connect(
        dsn, timeout=10, server_settings={'default_transaction_read_only': 'on'},
    )
    try:
        count = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        )
        if count:
            raise ValueError('EMPTY_TEST_DATABASE_REQUIRED')
    finally:
        await conn.close()


async def open_browser_platform(directory: Path, origin: str, resources: AsyncExitStack,
                                models: ModelBlocker) -> tuple[Platform, Principal]:
    """仅测试装配：在Platform启动前注入严格拒绝模型工厂，不实例化脚本模型。"""
    from xuanpu_server.controlplane import InMemoryControlPlane
    from xuanpu_server.identity import InMemoryIdentityStore
    from xuanpu_server.identity.config import IdentityConfig
    from xuanpu_server.identity.service import IdentitySettings
    from xuanpu_server.objectstore import LocalObjectStore
    from xuanpu_server.platform import ROOT_PRINCIPAL, Platform
    from xuanpu_server.runtime import LocalSandboxProvider
    from xuanpu_server.secrets import EncryptedVault, generate_master_key
    from xuanpu_server.tenancy import generate_root_key

    store = InMemoryControlPlane()
    _, root_hash = generate_root_key()
    data_dir = directory / 'data'
    data_dir.mkdir()
    platform = Platform(
        store=store, identity_store=InMemoryIdentityStore(store),
        identity_settings=IdentitySettings.from_config(origin, IdentityConfig(cookie_secure=False)),
        vault=EncryptedVault(store, keys={'k1': generate_master_key()}, primary='k1'),
        models=models, sandboxes=LocalSandboxProvider(), data_dir=data_dir,
        root_key_hash=root_hash, instance_id='inst-finance-browser-test',
        objects=LocalObjectStore(directory / 'objects'),
    )
    resources.push_async_callback(platform.close)
    await platform.start()
    await platform.create_tenant(ROOT_PRINCIPAL, tenant_id='acme', name='F9隔离验收租户')
    issued = await platform.issue_key(
        ROOT_PRINCIPAL, tenant_id='acme', name='browser-admin', role='tenant_admin',
    )
    return platform, await platform.authenticate(issued.key)


async def bootstrap_people(directory: Path, origin: str, resources: AsyncExitStack) -> tuple[
    Any, Any, dict[str, Any], dict[str, str], ModelBlocker,
]:
    """显式复用test-only身份夹具；动态对象不属于产品公共类型接口。"""
    identity = importlib.import_module('packages.xuanpu-server.tests.identity.conftest')
    blocker = ModelBlocker()
    platform, admin = await open_browser_platform(directory, origin, resources, blocker)
    world = SimpleNamespace(platform=platform, models=blocker, admin=admin)
    ident = identity.IdentityWorld(world=world, http=None, feishu=None)
    people, cookies = {}, {}
    for actor, name in [('owner', 'F8测试报销人'), ('manager', 'F8测试负责人'),
                        ('finance', 'F8测试财务复核')]:
        people[actor] = await ident.user(name=name)
        cookies[actor] = await ident.login(people[actor])
    world.platform.runtimes.get = blocker
    return world, ident, people, cookies, blocker


async def configure_finance(
    directory: Path, dsn: str, origin: str, port: int, key: Any,
    ident: Any, people: dict[str, Any],
) -> FinanceConfig:
    """使用现有test-only进程夹具构造财务配置，不连接外部供应商。"""
    from xuanpu_finance.config import FinanceConfig, GrantConfig
    from xuanpu_finance.expenses.models import ApprovalStep, ExpenseCategory, ExpensePolicy

    process = importlib.import_module('packages.xuanpu-finance.tests.test_process')
    config: FinanceConfig
    _, config = process.process_config(directory / 'finance', dsn, origin, key)
    config.port, config.synthetic = port, False
    config.issuer, config.audience = 'platform', 'xuanpu-finance'
    token_file = directory / 'finance/gateway-token'
    config.gateway.tenant_token_files = {'acme': token_file}
    token_file.write_text(await ident.system_key(role='tenant_member'))
    config.companies[0].tenant_id = 'acme'
    config.companies[0].name = 'F9隔离验收公司'
    roles: dict[str, Literal['collector', 'approver', 'reviewer']] = {
        'owner': 'collector', 'manager': 'approver', 'finance': 'reviewer',
    }
    config.grants = [
        GrantConfig(tenant_id='acme', user_id=person.user_id, legal_entity_id='company-a',
                    role=roles[actor]) for actor, person in people.items()
    ]
    config.expense_policies = [ExpensePolicy(
        tenant_id='acme', legal_entity_id='company-a', version=1, enabled=True,
        record_mode='internal_record', require_verified_invoice=False,
        categories=[ExpenseCategory(id='travel', name='交通费用')], cost_centers=[],
        steps=[ApprovalStep(id='manager', name='负责人审批',
                            approver_user_id=people['manager'].user_id),
               ApprovalStep(id='finance', name='财务复核',
                            approver_user_id=people['finance'].user_id)],
        exception_owner_user_id=people['finance'].user_id,
    )]
    for file in (directory / 'finance').iterdir():
        if file.is_file():
            file.chmod(0o600)
    config.profile = 'internal_records'
    return FinanceConfig.model_validate(config.model_dump())


async def configure_gateway(platform: Any, admin: Any, directory: Path, finance_url: str,
                            key: Any) -> None:
    """发布本次隔离业务包，并沿用平台签名出口访问真实财务HTTP。"""
    import yaml
    from cryptography.hazmat.primitives import serialization
    from xuanpu_server.config import CallContextConfig
    from xuanpu_server.manual_connectors import ManualConnectorService
    from xuanpu_server.runtime.call_context import HumanContextSigner
    from xuanpu_spec import pack_package

    await platform.vault.put('acme', 'finance-service-token', 'test-service', actor='acceptance')
    await platform.vault.put('acme', 'context-key', key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode(), actor='acceptance')
    signing = CallContextConfig(
        issuer='platform', keyId='test', privateKeySecret='context-key',
        allowedAudiences={finance_url: ['xuanpu-finance']},
    )
    pack = directory / 'browser-pack'
    shutil.copytree(ROOT / 'examples/finance', pack)
    manifest = yaml.safe_load((pack / 'xuanpu.yaml').read_text())
    for connector in manifest['spec']['connectors']:
        connector['baseUrl'] = finance_url
    (pack / 'xuanpu.yaml').write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False))
    await platform.packages.publish(admin, pack_package(pack), activate=True)
    platform.manual_connectors = ManualConnectorService(
        packages=platform.packages, vault=platform.vault, audit=platform.audit,
        signer=HumanContextSigner(config=signing, vault=platform.vault),
    )


def add_test_routes(app: FastAPI, ident: Any, cookies: dict[str, str], world: Any,
                    finance: Any, people: dict[str, Any], blocker: ModelBlocker) -> None:
    """仅向本次loopback应用注入测试bootstrap与无秘密证据，产品路由不变。"""
    from fastapi.responses import RedirectResponse

    @app.get('/__acceptance__/login/{actor}')
    async def login(actor: str) -> RedirectResponse:
        """仅本机一次性验收夹具签发测试员工会话，不加入产品路由。"""
        response = RedirectResponse('/console/', status_code=303)
        if actor in cookies:
            response.set_cookie(ident.cookie_name, cookies[actor], httponly=True, samesite='lax')
        return response

    @app.get('/__acceptance__/evidence')
    async def evidence() -> dict[str, Any]:
        """只输出测试库计数、随机测试员工编号及模型调用与尝试数。"""
        pool = finance.state.finance_store.pool
        counts = {table: await pool.fetchval('SELECT count(*) FROM ' + table) for table in [
            'finance_invoice', 'finance_settlement_operation', 'finance_compliance_operation',
            'finance_operation', 'finance_receipt',
        ]}
        calls = len(world.models.requests)
        return {'counts': counts, 'model_calls': calls,
                'model_attempts': blocker.attempts + calls,
                'blocked_attempts': blocker.blocked_attempts,
                'blocked_entries': dict(blocker.blocked_entries),
                'people': {actor: person.user_id for actor, person in people.items()}}


def create_fixtures(directory: Path) -> list[str]:
    """生成格式有效、明确非真实税务发票的本地测试PDF。"""
    from pypdf import PdfWriter

    fixtures = []
    for name in ['receipt-original.pdf', 'receipt-supplement.pdf']:
        path = directory / name
        writer = PdfWriter()
        writer.add_blank_page(width=240, height=120)
        writer.add_metadata({'/Title': 'F9格式有效测试附件，非真实税务发票：' + name})
        writer.write(path)
        fixtures.append(str(path))
    return fixtures


def publish_ready(ready_file: Path, payload: dict[str, Any]) -> None:
    """先写同目录0600临时文件，再用原子链接发布，绝不覆盖已有文件。"""
    with tempfile.NamedTemporaryFile(
        mode='w', encoding='utf-8', dir=ready_file.parent, prefix='.finance-ready-', delete=False,
    ) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, ready_file)
        finally:
            temporary.unlink(missing_ok=True)


async def announce_ready(ready_file: Path, payload: dict[str, Any],
                         resources: AsyncExitStack) -> None:
    """两个真实服务都健康后才发布入口，并将删除入口登记到资源栈。"""
    import httpx

    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        for url in [payload['origin'] + '/healthz', payload['finance_url'] + '/health']:
            if (await client.get(url)).status_code != 200:
                raise RuntimeError('BROWSER_ACCEPTANCE_SERVICE_NOT_READY')
    publish_ready(ready_file, payload)
    resources.callback(ready_file.unlink, missing_ok=True)


async def assemble(directory: Path, dsn: str, ready_file: Path, resources: AsyncExitStack) -> None:
    """隔离开发夹具只装配测试身份，业务流量仍经过真实TCP与签名认证。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from xuanpu_finance.readiness.offline import preflight
    from xuanpu_finance.runtime import open_runtime
    from xuanpu_server.api import create_app

    await ensure_empty_database(dsn)
    platform_port, finance_port = free_port(), free_port()
    origin = f'http://127.0.0.1:{platform_port}'
    finance_url = f'http://127.0.0.1:{finance_port}'
    world, ident, people, cookies, blocker = await bootstrap_people(directory, origin, resources)
    key = Ed25519PrivateKey.generate()
    config = await configure_finance(directory, dsn, origin, finance_port, key, ident, people)
    offline = await preflight(config)
    if offline['status'] != 'not_verified' or any(
            item['status'] != 'passed' for item in offline['checks']):
        raise RuntimeError('BROWSER_ACCEPTANCE_PREFLIGHT_BLOCKED')
    await configure_gateway(world.platform, world.admin, directory, finance_url, key)
    app = create_app(world.platform, console_dir=(
        ROOT / 'packages/xuanpu-console/src/xuanpu_console/static'
    ))
    finance = await resources.enter_async_context(open_runtime(config))
    add_test_routes(app, ident, cookies, world, finance, people, blocker)
    await resources.enter_async_context(listen(app, platform_port))
    await resources.enter_async_context(listen(finance, finance_port))
    payload = {
        'origin': origin, 'finance_url': finance_url,
        'logins': {actor: origin + '/__acceptance__/login/' + actor for actor in people},
        'fixtures': create_fixtures(directory),
        'environment': 'isolated_loopback_test', 'models': 'disabled',
        'profile': config.profile, 'preflight': offline,
    }
    await announce_ready(ready_file, payload, resources)


async def run(dsn: str, ready_file: Path) -> None:
    """等待显式结束信号后释放仅本入口创建的资源及就绪文件。"""
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        with tempfile.TemporaryDirectory(prefix='xuanpu-finance-f9-browser-') as raw:
            directory = Path(raw)
            directory.chmod(0o700)
            async with AsyncExitStack() as resources:
                await assemble(directory, dsn, ready_file, resources)
                print('BROWSER_ACCEPTANCE_READY', flush=True)
                await stopped.wait()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main() -> int:
    """任何初始化失败都只输出稳定诊断，不输出连接或秘密正文。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn-file', type=Path, required=True)
    parser.add_argument('--ready-file', type=Path, required=True)
    args = parser.parse_args()
    try:
        dsn = inputs(args.dsn_file, args.ready_file)
    except (ValueError, OSError):
        print('BROWSER_ACCEPTANCE_INPUT_INVALID')
        return 2
    except Exception:
        # 输入校验所依赖的驱动或模块初始化同样不得泄漏异常正文。
        print('BROWSER_ACCEPTANCE_FAILED')
        return 2
    try:
        asyncio.run(run(dsn, args.ready_file))
    except Exception:
        # 驱动、依赖、网络及清理异常均可能含秘密，只报告固定失败码。
        print('BROWSER_ACCEPTANCE_FAILED')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

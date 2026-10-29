"""明确opt-in的F15真实模型HTTP用户流程；只接受私有本机空测试库。"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import stat
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.acceptance.finance_model_runtime import (  # noqa: E402
    Budget,
    Evidence,
    RecordingFactory,
)


def private_text(path: Path) -> str:
    """拒绝链接、非私有权限和过大凭据，不回显内容。"""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536:
        raise ValueError('PRIVATE_INPUT_REQUIRED')
    return path.read_text().strip()


def model_config(path: Path) -> dict[str, Any]:
    """仅取私有文件models段，不复用部署、数据库或身份。"""
    models: dict[str, Any] = json.loads(private_text(path))['models']
    if models['protocol'] != 'codex_responses' or not models['api_key']:
        raise ValueError('MODEL_CONFIGURATION_INVALID')
    return models


async def platform_for(directory: Path, origin: str, finance_url: str, model: dict[str, Any],
                       budget: Budget, evidence: Evidence, resources: AsyncExitStack) -> Any:
    """构造生产平台并在首次运行时组装前注入真实模型和agent签名。"""
    from xuanpu_server.config import CallContextConfig
    from xuanpu_server.controlplane import InMemoryControlPlane
    from xuanpu_server.identity import InMemoryIdentityStore
    from xuanpu_server.identity.config import IdentityConfig
    from xuanpu_server.identity.service import IdentitySettings
    from xuanpu_server.objectstore import LocalObjectStore
    from xuanpu_server.platform import ROOT_PRINCIPAL, Platform
    from xuanpu_server.runtime import LocalSandboxProvider, RuntimeDefaults
    from xuanpu_server.runtime.models import GatewayConfig, GatewayModelClientFactory, ModelProfile
    from xuanpu_server.secrets import EncryptedVault, generate_master_key
    from xuanpu_server.tenancy import QuotaLimits, generate_root_key

    store = InMemoryControlPlane()
    vault = EncryptedVault(store, keys={'k1': generate_master_key()}, primary='k1')
    config = GatewayConfig(base_url=model['base_url'], api_key=model['api_key'],
        protocol=model['protocol'], profiles={tier: ModelProfile(profile['model'], 90)
                                              for tier, profile in model['tiers'].items()})
    models = RecordingFactory(GatewayModelClientFactory(config, vault), budget, evidence,
                              {k: v['model'] for k, v in model['tiers'].items()})
    _, root_hash = generate_root_key()
    platform = Platform(store=store, identity_store=InMemoryIdentityStore(store),
        identity_settings=IdentitySettings.from_config(origin, IdentityConfig(cookie_secure=False)),
        vault=vault, models=models, sandboxes=LocalSandboxProvider(), data_dir=directory / 'data',
        root_key_hash=root_hash, instance_id='inst-finance-f15',
        objects=LocalObjectStore(directory / 'objects'), proactive_scan_seconds=3600,
        defaults=RuntimeDefaults(max_session_tokens=200000, max_iterations=20, capture_requests=True),
        call_context=CallContextConfig(issuer='platform', keyId='test',
            privateKeySecret='context-key', allowedAudiences={finance_url: ['xuanpu-finance']}))
    resources.push_async_callback(platform.close)
    await platform.start()
    await platform.create_tenant(ROOT_PRINCIPAL, tenant_id='acme', name='F15合成测试租户',
                                 quota=QuotaLimits(max_daily_tokens=3_000_000))
    issued = await platform.issue_key(ROOT_PRINCIPAL, tenant_id='acme',
                                     name='f15-admin', role='tenant_admin')
    identity = importlib.import_module('packages.xuanpu-server.tests.identity.conftest')
    world = SimpleNamespace(platform=platform, models=models,
                            admin=await platform.authenticate(issued.key))
    ident = identity.IdentityWorld(world=world, http=None, feishu=None)
    people, cookies = {}, {}
    for actor, name in [('owner', 'F15合成报销人'), ('manager', 'F15合成负责人'),
                        ('finance', 'F15合成财务复核'), ('outsider', 'F15无财务授权员工')]:
        people[actor] = await ident.user(name=name)
        token = await ident.login(people[actor])
        cookies[actor] = ident.cookie(token)
        evidence.secrets.append(token)
    return SimpleNamespace(world=world, ident=ident, people=people, cookies=cookies,
                           models=models, platform=platform, session_actors={}, claim_source='model')


def signature_observer(finance: Any, key: Any, evidence: Evidence) -> list[dict[str, Any]]:
    """旁观实际TCP签名并独立验签，不替换真实认证裁决或请求。"""
    import jwt

    seen: list[dict[str, Any]] = []

    @finance.middleware('http')
    async def observe(request: Any, call_next: Any) -> Any:
        """只保留已独立验签的来源、用户与操作，绝不落token。"""
        token = request.headers.get('X-Xuanpu-Context')
        if token:
            claims = jwt.decode(token, key.public_key(), algorithms=['EdDSA'],
                                audience='xuanpu-finance', issuer='platform')
            seen.append({'path': request.url.path, 'method': request.method,
                         **{name: claims.get(name) for name in
                            ('origin', 'sub', 'session_id', 'request_sha256')}})
            evidence.save('signed-http.json', seen)
        return await call_next(request)

    return seen


def scenario_metadata(scenario: str) -> dict[str, Any]:
    """环境与结果共享显式来源契约，未知场景失败关闭。"""
    if scenario not in {'full', 'query-regression'}:
        raise ValueError('INVALID_SCENARIO')
    partial = scenario == 'query-regression'
    return {'scenario': scenario, 'counts_as_complete_journey': not partial,
            'fixture_source': 'public_human_http' if partial else 'model_drafts'}


async def assemble(directory: Path, dsn: str, model: dict[str, Any], budget: Budget,
                   evidence: Evidence, resources: AsyncExitStack,
                   *, scenario: str = 'full') -> Any:
    """真实TCP财务、独立PG/密文和实际平台Skill连接器装配。"""
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from xuanpu_finance.runtime import open_runtime
    from xuanpu_server.api import create_app

    from scripts.acceptance.finance_browser import (
        configure_finance,
        configure_gateway,
        ensure_empty_database,
        free_port,
        listen,
    )

    scope = scenario_metadata(scenario)
    await ensure_empty_database(dsn)
    platform_port, finance_port = free_port(), free_port()
    origin, finance_url = f'http://127.0.0.1:{platform_port}', f'http://127.0.0.1:{finance_port}'
    context = await platform_for(directory, origin, finance_url, model, budget, evidence, resources)
    context.scenario = scenario
    context.claim_source = 'model' if scope['counts_as_complete_journey'] else 'human_fixture'
    key = Ed25519PrivateKey.generate()
    config = await configure_finance(directory, dsn, origin, finance_port, key, context.ident,
                                     {k: v for k, v in context.people.items() if k != 'outsider'})
    await configure_gateway(context.platform, context.world.admin, directory, finance_url, key)
    finance = await resources.enter_async_context(open_runtime(config))
    context.seen = signature_observer(finance, key, evidence)
    context.finance, context.pool = finance, finance.state.finance_store.pool
    await resources.enter_async_context(listen(create_app(context.platform), platform_port))
    await resources.enter_async_context(listen(finance, finance_port))
    context.http = await resources.enter_async_context(httpx.AsyncClient(
        base_url=origin, trust_env=False, timeout=1250, follow_redirects=False))
    context.origin, context.directory, context.evidence = origin, directory, evidence
    evidence.save('environment.json', {'profile': config.profile,
        'identity': 'synthetic HTTP cookies, not SSO/browser',
        'people': {k: v.user_id for k, v in context.people.items()},
        'requested_models': {k: v['model'] for k, v in model['tiers'].items()},
        'protocol': model['protocol'], 'real_tcp': True, 'database': 'isolated_empty_finance_test',
        **scope})
    return context


async def run(options: Any) -> None:
    """任何异常均保留失败，资源清理之后不覆盖业务诊断。"""
    from scripts.acceptance.finance_model_focus import query_regression
    from scripts.acceptance.finance_model_journey import journey
    from scripts.acceptance.finance_runner import validate_dsn

    dsn, model = private_text(options.dsn_file), model_config(options.model_config)
    validate_dsn(dsn)
    if options.evidence.exists():
        raise ValueError('FRESH_EVIDENCE_DIRECTORY_REQUIRED')
    scope = scenario_metadata(options.scenario)
    options.state.mkdir(mode=0o700)
    evidence = Evidence(options.evidence, [dsn, model['api_key'], model['base_url']])
    partial = options.scenario == 'query-regression'
    evidence.result.update(scope)
    evidence.save('result.json', evidence.result)
    budget = None
    try:
        probe = json.loads((ROOT / 'docs/guides/releases/evidence/finance-f15-2026-10-23/model-probe.json').read_text())
        budget = Budget(options.ledger, options.journey, probe, max_streams=12 if partial else 120)
        async with asyncio.timeout(1200), AsyncExitStack() as resources:
            context = await assemble(options.state, dsn, model, budget, evidence, resources,
                                     scenario=options.scenario)
            await (query_regression(context) if partial else journey(context))
            evidence.complete(partial=partial)
    except BaseException as error:
        evidence.fail(type(error).__name__ + ':' + str(error))
        raise
    finally:
        if budget is not None:
            budget.checkpoint()
            evidence.save('budget-snapshot.json', budget.data)


def parse_options(argv: list[str] | None = None) -> argparse.Namespace:
    """默认完整验收，仅允许显式选定局部场景，无扩额参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-real-model', action='store_true', required=True)
    for name in ('dsn-file', 'model-config', 'state', 'evidence', 'ledger'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--journey', required=True)
    parser.add_argument('--scenario', choices=['full', 'query-regression'], default='full')
    return parser.parse_args(argv)


def main() -> int:
    """显式接受真实模型消费才运行，不从pytest或后台自动启动。"""
    options = parse_options()
    try:
        asyncio.run(run(options))
    except Exception as error:
        print('FINANCE_MODEL_FAILED:' + type(error).__name__)
        return 1
    print('FINANCE_MODEL_PARTIAL_PASSED' if options.scenario == 'query-regression'
          else 'FINANCE_MODEL_PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""执行降耗的离线预算、会话调度及定点来源边界。"""
from __future__ import annotations

import ast
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
runtime = importlib.import_module('scripts.acceptance.finance_model_runtime')
driver = importlib.import_module('scripts.acceptance.finance_model_journey')
cli = importlib.import_module('scripts.acceptance.finance_model')


def test_focused_budget_stops_before_thirteenth_stream(tmp_path: Path):
    """定点只可收紧到十二次，不花掉后续完整验收额度。"""
    budget = runtime.Budget(tmp_path / 'budget.json', 'focus', max_streams=12)
    for _ in range(12):
        budget.begin(1)
        budget.finish({'input_tokens': 1, 'output_tokens': 1}, 0)
    previous = budget.data['attempts']
    with pytest.raises(runtime.HarnessFailure, match='JOURNEY_STREAM_LIMIT'):
        budget.begin(1)
    assert budget.data['attempts'] == previous == 13
    assert budget.data['attempts'] < 240


@pytest.mark.parametrize('limit', [0, 121, True])
def test_invalid_budget_cannot_create_ledger(tmp_path: Path, limit):
    """非法局部额度不得落盘，更不能放大一百二十次原限制。"""
    path = tmp_path / 'budget.json'
    with pytest.raises(ValueError):
        runtime.Budget(path, 'focus', max_streams=limit)
    assert not path.exists()


def test_reopen_cannot_expand_persisted_local_budget(tmp_path: Path):
    """同名定点旅程不能通过重开默认模式变大上限。"""
    path = tmp_path / 'budget.json'
    runtime.Budget(path, 'focus', max_streams=12)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='JOURNEY_LIMIT_MISMATCH'):
        runtime.Budget(path, 'focus')
    assert path.read_bytes() == before


@pytest.mark.parametrize('failed', [False, True])
def test_partial_result_never_counts_as_complete(tmp_path: Path, failed: bool):
    """局部成功有独立状态，旧通过选择器也不会误计整条旅程。"""
    evidence = runtime.Evidence(tmp_path / 'evidence', [])
    if failed:
        evidence.fail('observed_failure')
    evidence.complete(partial=True)
    result = json.loads((evidence.directory / 'result.json').read_text())
    assert result['status'] == ('failed' if failed else 'partial_passed')
    assert result['counts_as_complete_journey'] is False


@pytest.mark.parametrize('scenario', ['full', 'query-regression'])
def test_explicit_scenario_and_default_are_bounded(scenario: str):
    """CLI显式选择场景，默认仍完整，不提供放大预算的开关。"""
    args = ['--allow-real-model', '--journey', 'test']
    for key in ['dsn-file', 'model-config', 'state', 'evidence', 'ledger']:
        args += ['--' + key, '/unused/' + key]
    assert cli.parse_options(args).scenario == 'full'
    assert cli.parse_options(args + ['--scenario', scenario]).scenario == scenario
    with pytest.raises(SystemExit):
        cli.parse_options(args + ['--scenario', 'unknown'])


def test_full_orchestration_keeps_same_actor_sessions():
    """调度契约锁住两处续会话，新的回查和跨角色入口不合并。"""
    tree = ast.parse((ROOT / 'scripts/acceptance/finance_model_journey.py').read_text())
    calls = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == 'message':
                label = ast.literal_eval(node.args[3])
                calls[label] = node
    for label, expected in [('06-manager-approve', 'manager_session'),
                            ('09-owner-unsafe', 'query_session')]:
        session = next(k.value for k in calls[label].keywords if k.arg == 'session')
        assert isinstance(session, ast.Name) and session.id == expected
    for label in ['04-manager-reject', '07-finance-approve',
                  '08-fresh-query', '10-outsider-unsafe']:
        assert not calls[label].keywords


@pytest.mark.asyncio
async def test_message_reuse_skips_router_and_rejects_wrong_actor(tmp_path: Path):
    """实际HTTP驱动续会话仅发业务消息，错身份在发请求前拒绝。"""
    requests = []

    def respond(request):
        """仅提供公开路由和SSE固定协议，不调用任何模型。"""
        requests.append(request)
        if request.url.path == '/v1/assistant/route':
            return httpx.Response(200, json={'session': {
                'package': 'finance', 'session_id': 'manager-session'}})
        if request.method == 'GET':
            return httpx.Response(200, json={'messages': []})
        event = {'kind': 'turn_completed', 'data': {'success': True, 'is_root': True}}
        return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\n')

    async with httpx.AsyncClient(base_url='http://offline',
                                 transport=httpx.MockTransport(respond)) as http:
        context = SimpleNamespace(http=http, models=SimpleNamespace(),
            people={actor: SimpleNamespace(user_id=actor) for actor in ['manager', 'owner']},
            cookies={actor: {'Cookie': 'session=' + actor} for actor in ['manager', 'owner']},
            session_actors={}, origin='http://offline',
            evidence=runtime.Evidence(tmp_path / 'evidence', []))
        session = await driver.message(context, 'manager', '查看待审', '04')
        await driver.message(context, 'manager', '继续审批', '06', session=session)
        assert sum(r.url.path == '/v1/assistant/route' for r in requests) == 1
        messages = [r for r in requests if r.url.path.endswith('/messages')]
        assert len(messages) == 2 and all('manager-session' in r.url.path for r in messages)
        assert all(r.headers['Cookie'] == 'session=manager' for r in messages)
        before = len(requests)
        with pytest.raises(runtime.HarnessFailure, match='SESSION_ACTOR_MISMATCH'):
            await driver.message(context, 'owner', '越权复用', 'bad', session=session)
        assert len(requests) == before


@pytest.mark.asyncio
async def test_fixture_claim_is_not_labeled_model_created(tmp_path: Path):
    """同一计数观察器按来源记录，人工fixture不能冒称模型创建。"""
    async def fetch(query):
        """提供仅一个ID的计数结果，实际HTTP契约另行集成验证。"""
        return [{'id': 'fixture-claim'}]

    async def respond(request):
        """模拟公开人工代理外层，不注入模型结果。"""
        return httpx.Response(200, json={'is_error': False, 'truncated': False,
                                       'body': {'id': 'fixture-claim'}})

    async with httpx.AsyncClient(base_url='http://offline',
                                 transport=httpx.MockTransport(respond)) as http:
        context = SimpleNamespace(http=http, pool=SimpleNamespace(fetch=fetch),
            claim_source='human_fixture', cookies={'owner': {'Cookie': 'test'}},
            origin='http://offline', evidence=runtime.Evidence(tmp_path / 'evidence', []))
        await driver.claim(context)
        assert [x['name'] for x in context.evidence.result['checks']] == [
            'one_fixture_created_claim']


@pytest.mark.asyncio
async def test_local_budget_mismatch_records_failed_scope(tmp_path: Path, monkeypatch):
    """旅程额度冲突也必须留failed，不留下看似仍运行的局部报告。"""
    ledger = tmp_path / 'budget.json'
    runtime.Budget(ledger, 'focus', max_streams=12)
    monkeypatch.setattr(cli, 'private_text', lambda path:
                        'postgresql://test:unused@127.0.0.1:5432/finance_test')
    monkeypatch.setattr(cli, 'model_config', lambda path: {'api_key': 'offline',
                                                        'base_url': 'http://offline'})
    options = SimpleNamespace(scenario='full', dsn_file=tmp_path / 'dsn',
        model_config=tmp_path / 'config', state=tmp_path / 'state',
        evidence=tmp_path / 'evidence', ledger=ledger, journey='focus')
    with pytest.raises(ValueError, match='JOURNEY_LIMIT_MISMATCH'):
        await cli.run(options)
    result = json.loads((options.evidence / 'result.json').read_text())
    assert result['status'] == 'failed'


@pytest.mark.asyncio
async def test_missing_probe_records_failure(tmp_path: Path, monkeypatch):
    """探针文件不可读也不能把已创建的结果遗留为running。"""
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setattr(cli, 'private_text', lambda path:
                        'postgresql://test:unused@127.0.0.1:5432/finance_test')
    monkeypatch.setattr(cli, 'model_config', lambda path: {'api_key': 'offline',
                                                        'base_url': 'http://offline'})
    options = SimpleNamespace(scenario='query-regression', dsn_file=tmp_path / 'dsn',
        model_config=tmp_path / 'config', state=tmp_path / 'state',
        evidence=tmp_path / 'evidence', ledger=tmp_path / 'budget', journey='missing-probe')
    with pytest.raises(FileNotFoundError):
        await cli.run(options)
    assert json.loads((options.evidence / 'result.json').read_text())['status'] == 'failed'


def test_scenario_contract_records_source():
    """环境和结果共用同一个显式来源契约，不悄悄将局部变成完整。"""
    assert cli.scenario_metadata('query-regression') == {'scenario': 'query-regression',
        'fixture_source': 'public_human_http', 'counts_as_complete_journey': False}
    assert cli.scenario_metadata('full')['counts_as_complete_journey'] is True
    with pytest.raises(ValueError):
        cli.scenario_metadata('unknown')


@pytest.mark.parametrize('package', ['examples/finance',
    'docs/superpowers/specs/finance-v1/pack'])
def test_model_query_argument_distinguishes_category_from_literal(package: str):
    """实际模型参数schema必须给出筛选范围，服务字面和null语义保持。"""
    from xuanpu_server.runtime.http_api import build_request
    from xuanpu_spec import parse_openapi

    api = parse_openapi((ROOT / package / 'apis/finance.yaml').read_text())
    op = api.operations['listExpenseClaimSummaries']
    query = op.input_schema()['properties']['q']
    assert '费用类别或旧事由' in query['description']
    assert '明确提供当前单号或当前事由的逐字值' in query['description']
    assert '不代表没有本人报销' in query['description']
    assert query['anyOf'] == [{'type': 'string', 'maxLength': 128}, {'type': 'null'}]
    for value, expected in [(None, None), ('当前事由交通', '当前事由交通')]:
        _, _, params, _, _ = build_request('http://offline', op,
            {'legal_entity_id': 'company-a', 'view': 'mine', 'q': value})
        assert dict(params).get('q') == expected
        if value is None:
            assert 'q' not in dict(params)


@pytest.mark.asyncio
async def test_human_fixture_rejects_full_before_any_http():
    """完整场景禁止误调用人工前置fixture。"""
    module = importlib.import_module('scripts.acceptance.finance_model_focus')
    with pytest.raises(runtime.HarnessFailure, match='FIXTURE_SCENARIO_REQUIRED'):
        await module.prepare_query_fixture(SimpleNamespace(scenario='full'))


@pytest.mark.asyncio
async def test_public_human_fixture_uses_zero_model_streams(tmp_path: Path):
    """实际独立PG和公开HTTP前置fixture不花模型，也不冒充模型草稿。"""
    from contextlib import AsyncExitStack

    import asyncpg

    from scripts.acceptance.finance_model_focus import prepare_query_fixture
    from scripts.acceptance.finance_runner import validate_dsn

    dsn = os.environ.get('FINANCE_TEST_DSN')
    if not dsn:
        pytest.skip('需显式独立FINANCE_TEST_DSN运行HTTP集成，不连接默认库')
    validate_dsn(dsn)
    database = 'finance_test_focus_' + uuid4().hex
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute('CREATE DATABASE "' + database + '"')
        parts = urlsplit(dsn)
        empty_dsn = urlunsplit(parts._replace(path='/' + database))
        model = {'base_url': 'http://127.0.0.1:1', 'api_key': 'offline-only',
            'protocol': 'codex_responses', 'tiers': {
                tier: {'model': 'offline-only'} for tier in ['fast', 'balanced', 'deep']}}
        evidence = runtime.Evidence(tmp_path / 'evidence', [dsn, empty_dsn])
        budget = runtime.Budget(tmp_path / 'budget.json', 'offline-fixture', max_streams=12)
        async with AsyncExitStack() as resources:
            context = await cli.assemble(tmp_path, empty_dsn, model, budget, evidence, resources,
                                         scenario='query-regression')
            environment = json.loads((evidence.directory / 'environment.json').read_text())
            assert environment['scenario'] == 'query-regression'
            assert environment['counts_as_complete_journey'] is False
            receipt, raw, file = await prepare_query_fixture(context)
            doc = await driver.final_facts(context)
            await driver.retained_original(context, receipt, raw, file, doc)
            assert doc['status'] == 'approved'
            assert budget.local['attempts'] == 0 and budget.data['attempts'] == 1
            assert len([c for c in context.seen if c['origin'] == 'agent']) == 0
            assert not any(x['name'] == 'one_model_created_claim'
                           for x in evidence.result['checks'])
            assert all(x['passed'] for x in evidence.result['checks'])
    finally:
        await admin.execute('DROP DATABASE IF EXISTS "' + database + '"')
        await admin.close()

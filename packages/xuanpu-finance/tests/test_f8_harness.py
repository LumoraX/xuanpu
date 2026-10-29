"""验证浏览器隔离入口的输入拒绝、就绪发布和秘密保护。"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from types import ModuleType


def load_browser() -> ModuleType:
    """从固定源码路径加载入口，避免依赖pytest的工作目录导入路径。"""
    path = Path(__file__).resolve().parents[3] / 'scripts/acceptance/finance_browser.py'
    spec = importlib.util.spec_from_file_location('finance_browser', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


browser = load_browser()


@pytest.fixture
def private_dsn(tmp_path: Path) -> Path:
    """提供不会实际连接的私有本机测试库描述。"""
    path = tmp_path / 'dsn'
    path.write_text('postgresql://sample:private-value@127.0.0.1:1/finance_test_browser')
    path.chmod(0o600)
    return path


def test_browser_harness_rejects_non_test_database_before_listening(tmp_path: Path) -> None:
    """不合格数据库必须明确失败且不写ready文件或输出凭据。"""
    script = Path(__file__).resolve().parents[3] / 'scripts/acceptance/finance_browser.py'
    assert script.is_file(), '缺少F8实际浏览器验收入口'
    dsn = tmp_path / 'dsn'
    dsn.write_text('postgresql://sample:private-value@127.0.0.1:1/company_finance')
    dsn.chmod(0o600)
    ready = tmp_path / 'ready.json'
    result = subprocess.run(
        [sys.executable, str(script), '--dsn-file', str(dsn), '--ready-file', str(ready)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2
    assert 'BROWSER_ACCEPTANCE_INPUT_INVALID' in result.stdout
    assert 'private-value' not in result.stdout + result.stderr
    assert not ready.exists()


@pytest.mark.parametrize('permissions', [0o644, 0o660, 0o400])
def test_inputs_reject_non_private_mode(private_dsn: Path, permissions: int) -> None:
    """DSN必须是显式0600的普通文件。"""
    private_dsn.chmod(permissions)
    with pytest.raises(ValueError, match='INPUT_INVALID'):
        browser.inputs(private_dsn, private_dsn.parent / 'ready')


def test_inputs_reject_symlink(private_dsn: Path) -> None:
    """DSN符号链接不能替代调用者明确提供的私有普通文件。"""
    link = private_dsn.parent / 'dsn-link'
    link.symlink_to(private_dsn)
    with pytest.raises(ValueError, match='INPUT_INVALID'):
        browser.inputs(link, private_dsn.parent / 'ready')


@pytest.mark.parametrize('stage', ['inputs', 'run'])
def test_initialization_error_is_redacted(
    private_dsn: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    """任意装配异常以稳定诊断退出，不能输出异常内的凭据或堆栈。"""
    async def fail(dsn: str, ready_file: Path) -> None:
        """模拟驱动或服务初始化带秘密的异常。"""
        raise RuntimeError('driver rejected ' + dsn)

    def fail_inputs(dsn_file: Path, ready_file: Path) -> str:
        """模拟输入依赖初始化异常带有秘密。"""
        raise RuntimeError('private-value')

    monkeypatch.setattr(browser, stage, fail if stage == 'run' else fail_inputs)
    monkeypatch.setattr(sys, 'argv', ['browser', '--dsn-file', str(private_dsn),
                                    '--ready-file', str(private_dsn.parent / 'ready')])
    assert browser.main() == 2
    output = capsys.readouterr()
    assert output.out == 'BROWSER_ACCEPTANCE_FAILED\n'
    assert output.err == ''
    assert 'private-value' not in output.out + output.err


@pytest.fixture
def readiness_probe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """用HTTP传输替身观察真实客户端发出的健康探测路径。"""
    requested: list[str] = []
    client_type = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        """仅记录地址并按测试标志模拟某一服务不健康。"""
        requested.append(str(request.url))
        status = 503 if 'unready' in request.url.host else 200
        return httpx.Response(status, request=request)

    def client(**kwargs: object) -> httpx.AsyncClient:
        """保留客户端超时等设置，仅替换网络传输层。"""
        return client_type(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return requested


@pytest.mark.parametrize('failed_service', ['platform', 'finance'])
async def test_ready_failure_never_publishes(
    tmp_path: Path, readiness_probe: list[str], failed_service: str,
) -> None:
    """任一服务健康探测失败都不能留下可用入口或临时就绪文件。"""
    payload = {'origin': 'http://platform.test', 'finance_url': 'http://finance.test'}
    key = 'origin' if failed_service == 'platform' else 'finance_url'
    payload[key] = 'http://unready.test'
    ready = tmp_path / 'ready'
    async with AsyncExitStack() as resources:
        with pytest.raises(RuntimeError, match='NOT_READY'):
            await browser.announce_ready(ready, payload, resources)
        assert not ready.exists()
        assert list(tmp_path.iterdir()) == []
    assert readiness_probe


async def test_ready_published_after_both_probes_and_cleaned(
    tmp_path: Path, readiness_probe: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个服务健康才原子发布0600入口，退出资源栈后入口消失。"""
    ready = tmp_path / 'ready'
    payload = {'origin': 'http://platform.test', 'finance_url': 'http://finance.test'}
    publish = browser.publish_ready

    def checked_publish(path: Path, data: dict[str, str]) -> None:
        """在文件实际发布这一刻断言两个健康探测已完成。"""
        assert readiness_probe == ['http://platform.test/healthz', 'http://finance.test/health']
        assert not path.exists()
        publish(path, data)

    monkeypatch.setattr(browser, 'publish_ready', checked_publish)
    async with AsyncExitStack() as resources:
        await browser.announce_ready(ready, payload, resources)
        assert readiness_probe == ['http://platform.test/healthz', 'http://finance.test/health']
        assert ready.stat().st_mode & 0o777 == 0o600
        assert 'platform.test' in ready.read_text()
        assert list(tmp_path.iterdir()) == [ready]
    assert not ready.exists()


async def test_ready_does_not_overwrite_existing_file(
    tmp_path: Path, readiness_probe: list[str],
) -> None:
    """发布时发生竞争也不能覆盖调用者已有的就绪文件。"""
    ready = tmp_path / 'ready'
    ready.write_text('existing')
    async with AsyncExitStack() as resources:
        with pytest.raises(FileExistsError):
            await browser.announce_ready(
                ready, {'origin': 'http://platform.test', 'finance_url': 'http://finance.test'}, resources,
            )
    assert ready.read_text() == 'existing'
    assert list(tmp_path.iterdir()) == [ready]


async def test_model_attempts_are_counted_even_when_rejected() -> None:
    """模型拒绝必须保留尝试数，不能因为未调用模型客户端而误报零。"""
    blocker = browser.ModelBlocker()
    for _ in range(2):
        with pytest.raises(RuntimeError, match='MODEL_DISABLED'):
            await blocker('test')
    assert blocker.attempts == 2


async def test_failed_assembly_closes_registered_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中途装配失败也释放已注册资源并清理本次发布的入口。"""
    closed: list[str] = []
    ready = tmp_path / 'ready'

    async def fail(directory: Path, dsn: str, ready_file: Path, resources: AsyncExitStack) -> None:
        """模拟创建资源后初始化失败，不打开真实运行时或数据库。"""
        resources.callback(closed.append, 'closed')
        ready_file.write_text('owned')
        resources.callback(ready_file.unlink)
        raise RuntimeError('initialization failed')

    monkeypatch.setattr(browser, 'assemble', fail)
    with pytest.raises(RuntimeError, match='initialization failed'):
        await browser.run('unused', ready)
    assert closed == ['closed']
    assert not ready.exists()


async def test_nonempty_database_is_readonly_and_connection_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发现任何用户表即拒绝，检查连接为只读且失败后关闭。"""
    import asyncpg

    from scripts.acceptance import finance_runner

    events: list[str] = []

    class Connection:
        """模拟已有用户表的只读数据库连接。"""

        async def fetchval(self, query: str) -> int:
            """确认检查范围包含非public业务schema。"""
            assert "table_schema='public'" not in query
            events.append('queried')
            return 1

        async def close(self) -> None:
            """记录连接已可靠释放。"""
            events.append('closed')

    async def check_database(dsn: str) -> None:
        """隔离已单独测试的实际库身份验证。"""
        events.append('identity_checked')

    async def connect(dsn: str, **kwargs: object) -> Connection:
        """检查空库探测使用只读事务默认设置。"""
        assert kwargs['server_settings'] == {'default_transaction_read_only': 'on'}
        return Connection()

    monkeypatch.setattr(finance_runner, 'check_database', check_database)
    monkeypatch.setattr(asyncpg, 'connect', connect)
    with pytest.raises(ValueError, match='EMPTY_TEST_DATABASE_REQUIRED'):
        await browser.ensure_empty_database('unused')
    assert events == ['identity_checked', 'queried', 'closed']


async def test_database_rejected_before_identity_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空库门禁失败时不能创建测试身份、文件或监听服务。"""
    async def reject(dsn: str) -> None:
        """模拟有数据的测试库。"""
        raise ValueError('EMPTY_TEST_DATABASE_REQUIRED')

    async def forbidden(*args: object) -> None:
        """任何身份装配调用都表示输入门禁失效。"""
        pytest.fail('未通过数据库门禁不应创建测试身份')

    monkeypatch.setattr(browser, 'ensure_empty_database', reject)
    monkeypatch.setattr(browser, 'bootstrap_people', forbidden)
    async with AsyncExitStack() as resources:
        with pytest.raises(ValueError, match='EMPTY_TEST_DATABASE_REQUIRED'):
            await browser.assemble(tmp_path, 'unused', tmp_path / 'ready', resources)
    assert list(tmp_path.iterdir()) == []


def test_failed_atomic_serialization_leaves_no_ready_or_temporary(tmp_path: Path) -> None:
    """发布序列化失败不能留下半写入口或临时文件。"""
    with pytest.raises(TypeError):
        browser.publish_ready(tmp_path / 'ready', {'invalid': object()})
    assert list(tmp_path.iterdir()) == []


async def test_bootstrap_routes_report_blocked_attempt_without_secrets() -> None:
    """仅注入本机测试应用的路由可用，证据区分实际调用和被拦截尝试。"""
    from types import SimpleNamespace

    from fastapi import FastAPI

    class Pool:
        """只为证据路由提供计数，不连接真实业务库。"""

        async def fetchval(self, query: str) -> int:
            """输出固定测试计数。"""
            return 0

    app = FastAPI()
    blocker = browser.ModelBlocker()
    with pytest.raises(RuntimeError):
        await blocker()
    browser.add_test_routes(
        app, SimpleNamespace(cookie_name='test-cookie'), {'owner': 'private-cookie'},
        SimpleNamespace(models=SimpleNamespace(requests=[])),
        SimpleNamespace(state=SimpleNamespace(finance_store=SimpleNamespace(pool=Pool()))),
        {'owner': SimpleNamespace(user_id='test-only-user')}, blocker,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://127.0.0.1') as client:
        login = await client.get('/__acceptance__/login/owner')
        assert login.status_code == 303
        assert 'HttpOnly' in login.headers['set-cookie']
        evidence = await client.get('/__acceptance__/evidence')
        assert evidence.status_code == 200
        assert evidence.json()['model_calls'] == 0
        assert evidence.json()['model_attempts'] == 1
        assert evidence.json()['blocked_attempts'] == 1
        assert evidence.json()['blocked_entries'] == {'runtime': 1, 'factory': 0, 'session': 0}
        assert 'private-cookie' not in evidence.text


DUAL_LISTENER_PROCESS = '''
import asyncio
import sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
sys.path.insert(0, sys.argv.pop(1))
from scripts.acceptance import finance_browser as browser

async def assemble(directory, dsn, ready_file, resources):
    app = FastAPI()
    @app.get('/healthz')
    @app.get('/health')
    async def health():
        return {'status': 'ok'}
    @app.get('/events')
    async def events():
        async def stream():
            yield 'data: ready\\n\\n'
            await asyncio.Event().wait()
        return StreamingResponse(stream(), media_type='text/event-stream')
    ports = [browser.free_port(), browser.free_port()]
    for port in ports:
        await resources.enter_async_context(browser.listen(app, port))
    await browser.announce_ready(ready_file, {
        'origin': 'http://127.0.0.1:' + str(ports[0]),
        'finance_url': 'http://127.0.0.1:' + str(ports[1]),
    }, resources)

browser.assemble = assemble
raise SystemExit(browser.main())
'''


def test_sigterm_stops_two_real_listeners_with_open_stream(
    tmp_path: Path, private_dsn: Path,
) -> None:
    """真实双服务和未结束事件流收到SIGTERM后及时退出，清除就绪入口。"""
    import json
    import signal
    import time

    ready = tmp_path / 'ready'
    process = subprocess.Popen(
        [sys.executable, '-c', DUAL_LISTENER_PROCESS, str(Path(__file__).resolve().parents[3]),
         '--dsn-file', str(private_dsn), '--ready-file', str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), '双服务未发布就绪入口'
        urls = json.loads(ready.read_text())
        with httpx.Client(trust_env=False, timeout=5) as client:
            assert client.get(urls['origin'] + '/healthz').status_code == 200
            assert client.get(urls['finance_url'] + '/health').status_code == 200
            with client.stream('GET', urls['origin'] + '/events') as response:
                assert response.status_code == 200
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stdout + stderr
        assert stdout == 'BROWSER_ACCEPTANCE_READY\n'
        assert stderr == ''
        assert not ready.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize('entry', ['factory', 'session', 'proactive'])
async def test_bootstrap_blocks_model_provider_and_proactive_sampling(
    tmp_path: Path, entry: str,
) -> None:
    """平台构造时即禁模型：工厂、直接会话及后台主动采样都必须真实失败。"""
    from taifeng import CancellationToken

    async with AsyncExitStack() as resources:
        world, _, people, _, blocker = await browser.bootstrap_people(
            tmp_path, 'http://127.0.0.1:1', resources,
        )
        with pytest.raises(RuntimeError, match='MODEL_DISABLED'):
            if entry == 'factory':
                await world.models.create('acme', 'fast')
            elif entry == 'session':
                world.models.session(cancel=CancellationToken())
            else:
                await world.platform.assistant.sample_proactive(
                    'acme', people['owner'].user_id, '{"evidence":[]}',
                )
        assert blocker.attempts == 1
        assert len(world.models.requests) == 0
        assert blocker.blocked_attempts == 1
        assert sum(blocker.blocked_entries.values()) == 1


async def test_blocked_background_assessment_records_failure_and_manual_work_continues(
    tmp_path: Path,
) -> None:
    """真实后台评估保留拒绝错误，普通人工记录仍可创建且无模型成功请求。"""
    from taifeng import ModelClient
    from xuanpu_server.runtime.models import ModelClientFactory
    from xuanpu_server.tenancy import Principal

    async with AsyncExitStack() as resources:
        world, _, people, _, blocker = await browser.bootstrap_people(
            tmp_path, 'http://127.0.0.1:1', resources,
        )
        assert isinstance(blocker, ModelClientFactory)
        assert isinstance(blocker, ModelClient)
        owner = people['owner'].user_id
        who = Principal('tenant_member', 'acme', None, owner)
        await world.platform.workspace.create(
            who, 'todo', {'title': '测试本人已记录待办'}, 'background-evidence',
        )
        assert await world.platform.proactive.evaluate_owner('acme', owner) is False
        async with world.platform.proactive.transaction('acme', owner) as state:
            assert state.scan_state['last_error'] == 'RuntimeError'
            assert state.scan_state['attempts'] == 1
            assert not state.followups
        manual = await world.platform.workspace.create(
            who, 'todo', {'title': '后台拒绝后仍可人工创建'}, 'manual-after-model-block',
        )
        assert manual['data']['title'] == '后台拒绝后仍可人工创建'
        assert blocker.blocked_entries == {'runtime': 0, 'factory': 1, 'session': 0}
        assert len(world.models.requests) == 0

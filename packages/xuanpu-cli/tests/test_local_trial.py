"""本地启动工具的私有配置与进程边界测试。"""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))


def support():
    """在测试执行时加载新工具，让缺实现表现为断言失败。"""
    path = ROOT / 'scripts/local_trial_support.py'
    assert path.exists(), '本地部署配置支持模块尚未实现'
    spec = importlib.util.spec_from_file_location('local_trial_support', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample():
    """只用虚构凭据构造输入。"""
    return {'model': {'base_url': 'https://model.example/v1', 'api_key': 'fake-key', 'name': 'demo'},
            'feishu': {'app_id': 'cli_demo', 'app_secret': 'fake-secret', 'encrypt_key': '', 'verification_token': ''}}


def test_persistent_private_configuration(tmp_path):
    """重启保留根凭据与保管库密钥，数据库持久化且会话留本机。"""
    mod = support()
    first = mod.prepare(tmp_path / 'runtime', sample(), 8420, 8431, 55432)
    second = mod.prepare(tmp_path / 'runtime', sample(), 8420, 8431, 55432)
    assert first == second
    config = json.loads((tmp_path / 'runtime/config.json').read_text())
    assert config['models']['protocol'] == 'codex_responses'
    assert config['control_plane']['backend'] == 'postgres'
    assert config['state']['backend'] == 'local'
    assert config['identity']['feishu_open_base'] == 'https://open.feishu.cn'
    assert (tmp_path / 'runtime/config.json').stat().st_mode & 0o777 == 0o600
    assert (tmp_path / 'runtime').stat().st_mode & 0o777 == 0o700
    assert mod.feishu_payload(sample()['feishu'])['encrypt_key_ref'] is None
    assert mod.feishu_payload(sample()['feishu'])['verification_token_ref'] is None


def test_changed_input_preserves_master_keys_and_root(tmp_path):
    """停机后更新模型配置，保留数据解密密钥与管理员凭据。"""
    mod = support()
    first = mod.prepare(tmp_path, sample(), 8420, 8431, 55432)
    before = json.loads((tmp_path / 'config.json').read_text())
    changed = sample()
    changed['model']['api_key'] = 'other-secret'
    second = mod.prepare(tmp_path, changed, 8420, 8431, 55432)
    after = json.loads((tmp_path / 'config.json').read_text())
    assert after['models']['api_key'] == 'other-secret'
    assert after['master_keys'] == before['master_keys']
    assert after['root_key_hash'] == before['root_key_hash']
    assert first['root'] == second['root']
    assert first['postgres_password'] == second['postgres_password']
    assert not second['bootstrap_complete']


def test_busy_port_not_adopted():
    """不把占用端口的服务当成本工具创建的服务。"""
    mod = support()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        with pytest.raises(RuntimeError, match='端口'):
            mod.require_free_port(sock.getsockname()[1])


def test_closed_server_time_wait_allows_immediate_restart():
    """服务器主动断开留下TIME_WAIT时，端口仍能立即供新服务器监听。"""
    mod = support()
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        address = server.getsockname()
        with socket.create_connection(address, timeout=2) as client:
            accepted, _ = server.accept()
            accepted.close()
            assert client.recv(1) == b''
    mod.require_free_port(address[1])


@pytest.mark.parametrize('address', ['127.0.0.1', '0.0.0.0'])
def test_reusable_listening_port_still_rejected(address):
    """即使已有服务启用地址复用，也不能把其监听端口当作空闲。"""
    mod = support()
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((address, 0))
        server.listen(1)
        with pytest.raises(RuntimeError, match='端口'):
            mod.require_free_port(server.getsockname()[1])


def test_explicit_chat_and_optional_event_keys(tmp_path):
    """保留显式协议选择，独立保留已有事件密钥。"""
    mod = support()
    data = sample()
    data['model']['protocol'] = 'chat'
    mod.prepare(tmp_path, data, 8420, 8431, 55432)
    assert json.loads((tmp_path / 'config.json').read_text())['models']['protocol'] == 'chat'
    data['feishu']['verification_token'] = 'fake-token'
    accepted = mod.validate_input(data)
    payload = mod.feishu_payload(accepted['feishu'])
    assert payload['encrypt_key_ref'] is None
    assert payload['verification_token_ref'] == 'feishu-verification-token'


def test_symlink_runtime_rejected(tmp_path):
    """拒绝通过符号链接写入别人的目录。"""
    mod = support()
    target = tmp_path / 'target'
    target.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match='符号链接'):
        mod.prepare(link, sample(), 8420, 8431, 55432)


def launcher():
    """装入启动器并确认生命周期实现已经存在。"""
    path = ROOT / 'scripts/local_trial.py'
    assert path.exists(), '本地部署生命周期尚未实现'
    spec = importlib.util.spec_from_file_location('local_trial', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_process_identity_and_stop(tmp_path):
    """真实子进程只在命令与启动时间均匹配时才允许停止。"""
    import asyncio
    mod = launcher()
    directory = tmp_path.resolve()
    process = await mod.spawn(directory, 'probe', [sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        assert await mod.owned(process)
        wrong = dict(process, started='different')
        assert not await mod.owned(wrong)
        with pytest.raises(RuntimeError, match='归属'):
            await mod.stop(wrong)
        assert await mod.owned(process)
    finally:
        await mod.stop(process)
    await asyncio.sleep(0.05)
    assert not await mod.owned(process)


async def test_bootstrap_repeated_does_not_reissue_or_reset(tmp_path):
    """已部署环境再次启动只验凭据，不重发 key 或覆盖保管库/配置。"""
    import httpx
    mod = launcher()
    calls = []
    def response(request):
        """仅提供实际 API 验证凭据的接口。"""
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={'tenant_id': 'acme', 'role': 'tenant_admin'})
    credentials = {'key': 'fake-admin', 'bootstrap_complete': True}
    async with httpx.AsyncClient(transport=httpx.MockTransport(response), base_url='http://local') as client:
        await mod.bootstrap(client, tmp_path, credentials, sample(), 8431)
    assert calls == [('GET', '/v1/me')]


def test_model_root_url_only_gets_default_path():
    """默认地址补齐不破坏网关的自定义路径。"""
    mod = support()
    data = sample()
    data['model']['base_url'] = 'https://model.example'
    assert mod.validate_input(data)['model']['base_url'] == 'https://model.example/v1'
    data['model']['base_url'] = 'https://model.example/custom'
    assert mod.validate_input(data)['model']['base_url'] == 'https://model.example/custom'


async def test_docker_label_is_not_enough_for_ownership(tmp_path, monkeypatch):
    """同标签但映射错误的容器不得启动，避免连错数据库。"""
    mod = launcher()
    monkeypatch.setattr(mod.shutil, 'which', lambda _: '/bin/docker')
    async def command(*args, **kwargs):
        """伪造已存在却端口不符合部署配置的 Docker 资源。"""
        if 'info' in args:
            return 0, '27'
        if 'volume' in args:
            return 0, json.dumps([{'Labels': {mod.LABEL: 'owner'}}])
        if 'inspect' in args:
            return 0, json.dumps([{'Config': {'Labels': {mod.LABEL: 'owner'}, 'Image': 'postgres:16'},
                'State': {'Running': True}, 'HostConfig': {'PortBindings': {'5432/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '9999'}]}},
                'Mounts': []}])
        return 0, ''
    monkeypatch.setattr(mod, 'command', command)
    with pytest.raises(RuntimeError, match='配置'):
        await mod.docker(tmp_path, {'owner': 'owner'}, 25432, start=True)


async def test_repeat_up_is_idempotent(tmp_path, monkeypatch, capsys):
    """同配置的运行实例可再次 up，不改配置也不再启动进程。"""
    from argparse import Namespace
    mod = launcher()
    sup = support()
    data = sample()
    sup.prepare(tmp_path, data, 8420, 8431, 25432)
    sup.write_private(tmp_path / 'input-source.json', data)
    state = {'platform': {'pid': 123}, 'tickets': {'pid': 124}}
    sup.write_private(tmp_path / 'processes.json', state)
    async def identity(pid):
        """提供属于当前部署的两个运行进程。"""
        return {'pid': pid}
    monkeypatch.setattr(mod, 'identity', identity)
    args = Namespace(input=tmp_path / 'input-source.json', port=8420, ticket_port=8431, postgres_port=25432)
    await mod.up(args, tmp_path)
    assert '/console/' in capsys.readouterr().out


@pytest.mark.parametrize('failure', ['exception', 'cancel', 'stubborn'])
async def test_spawn_registration_failure_reaps_child(tmp_path, monkeypatch, failure):
    """身份登记异常或被取消，都先回收真实子进程，再保留原始失败。"""
    import asyncio
    mod = launcher()
    children = []
    registering = asyncio.Event()
    original_create = asyncio.create_subprocess_exec
    original_error = RuntimeError('身份读取失败')

    async def create(*args, **kwargs):
        """记录真实创建的进程，以验证退出后无孤儿。"""
        child = await original_create(*args, **kwargs)
        children.append(child)
        return child

    async def broken_identity(pid):
        """将失败固定在创建成功之后的登记阶段。"""
        registering.set()
        if failure == 'stubborn':
            for _ in range(100):
                if 'ready' in (tmp_path / 'registration.log').read_text():
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError('测试子进程未安装信号处理器')
        if failure != 'cancel':
            raise original_error
        await asyncio.Event().wait()

    monkeypatch.setattr(mod.asyncio, 'create_subprocess_exec', create)
    monkeypatch.setattr(mod, 'identity', broken_identity)
    program = 'import time; time.sleep(60)'
    if failure == 'stubborn':
        program = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready", flush=True); time.sleep(60)'
    task = asyncio.create_task(mod.spawn(
        tmp_path, 'registration', [sys.executable, '-c', program]))
    try:
        await asyncio.wait_for(registering.wait(), timeout=5)
        if failure == 'cancel':
            task.cancel('登记被取消')
            with pytest.raises(asyncio.CancelledError, match='登记被取消'):
                await task
        else:
            with pytest.raises(RuntimeError) as raised:
                await task
            assert raised.value is original_error
        assert len(children) == 1
        assert children[0].returncode is not None, '登记失败后仍有未退出子进程'
        if failure == 'stubborn':
            import signal
            assert children[0].returncode == -signal.SIGKILL
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()


@pytest.mark.parametrize('releases', [True, False])
async def test_owned_stopped_docker_waits_for_port_release(tmp_path, monkeypatch, releases):
    """受控容器重启容忍短暂释放延迟，持续占用仍有界拒绝。"""
    mod = launcher()
    monkeypatch.setattr(mod.shutil, 'which', lambda _: '/bin/docker')
    starts = []
    attempts = []
    waits = []

    async def command(*args, **kwargs):
        """提供归属、端口和卷都匹配的已停止容器。"""
        if 'info' in args:
            return 0, '27'
        if 'volume' in args:
            return 0, json.dumps([{'Labels': {mod.LABEL: 'owner'}}])
        if 'inspect' in args:
            return 0, json.dumps([{'Config': {'Labels': {mod.LABEL: 'owner'}, 'Image': 'postgres:16'},
                'State': {'Running': False}, 'HostConfig': {'PortBindings': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '25432'}]}},
                'Mounts': [{'Name': 'xuanpu-local-data-owner', 'Destination': '/var/lib/postgresql/data'}]}])
        if 'start' in args:
            starts.append(args)
        return 0, ''

    def port(port_number):
        """模拟 Docker 端口在两次探测后释放，或一直被占用。"""
        attempts.append(port_number)
        if not releases or len(attempts) <= 2:
            raise RuntimeError('端口尚未释放')

    async def sleep(seconds):
        """记录有界退避，不让单测实际等待五秒。"""
        waits.append(seconds)

    monkeypatch.setattr(mod, 'command', command)
    monkeypatch.setattr(mod, 'require_free_port', port)
    monkeypatch.setattr(mod.asyncio, 'sleep', sleep)
    if releases:
        await mod.docker(tmp_path, {'owner': 'owner'}, 25432, start=True)
        assert len(starts) == 1
        assert len(attempts) == 3
        assert len(waits) == 2
    else:
        with pytest.raises(RuntimeError, match='端口'):
            await mod.docker(tmp_path, {'owner': 'owner'}, 25432, start=True)
        assert starts == []
        assert 1 < len(attempts) <= 51
        assert 0 < sum(waits) <= 5.01

"""可重复的本地真实部署：up、status、down；秘密只存私有运行目录。"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import signal
import sys
from pathlib import Path

import httpx
import yaml
from local_trial_support import (
    feishu_payload,
    prepare,
    private_dir,
    read_private,
    require_free_port,
    validate_input,
    write_private,
)
from xuanpu_spec import pack_package

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'cn.xuanpu.local-owner'


async def command(*args: str, timeout: float = 30) -> tuple[int, str]:
    """捕获外部命令输出；失败不回显可能含秘密的错误正文。"""
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    return process.returncode, stdout.decode().strip()


async def identity(pid: int) -> dict | None:
    """用进程启动时间与完整命令防止把复用的 PID 当作自己的服务。"""
    code, output = await command('ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command=')
    if code or not output:
        return None
    return {'pid': pid, 'started': output[:24], 'command': output[24:].strip()}


async def owned(record: dict) -> bool:
    """只接受与记录完全一致的进程。"""
    return await identity(record['pid']) == record


async def spawn(directory: Path, name: str, args: list[str]) -> dict:
    """后台启动已知命令并登记身份，失败不留下未登记进程。"""
    with (directory / (name + '.log')).open('ab') as log:
        os.chmod(log.name, 0o600)
        process = await asyncio.create_subprocess_exec(*args, cwd=ROOT, stdout=log,
                                                       stderr=asyncio.subprocess.STDOUT,
                                                       start_new_session=True)
    try:
        record = await identity(process.pid)
        if record is None:
            raise RuntimeError('服务启动失败；请检查私有运行目录日志')
        return record
    except BaseException:
        # 创建句柄证明归属；登记失败时不能依赖尚未写入的 PID 记录清理。
        cleanup = asyncio.create_task(reap_unregistered(process))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # 重复取消不能打断回收；完成后仍重抛最初的异常或取消。
                continue
        cleanup.result()
        raise


async def reap_unregistered(process: asyncio.subprocess.Process) -> None:
    """优雅停止刚创建但未登记的子进程，超时强制结束并等待回收。"""
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            # 子进程可能恰好自行退出，仍须走 wait 收集退出状态。
            await process.wait()
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            # 超时与自行退出竞争时仍由下面的 wait 回收。
            await process.wait()
            return
        await process.wait()


async def stop(record: dict) -> None:
    """核实归属后优雅停止，不对未知或已复用的 PID 发信号。"""
    current = await identity(record['pid'])
    if current is None:
        return
    if current != record:
        raise RuntimeError('进程归属已变化，拒绝停止')
    os.kill(record['pid'], signal.SIGTERM)
    for _ in range(300):
        if not await owned(record):
            return
        await asyncio.sleep(0.5)
    raise RuntimeError('服务尚未完成优雅退出；保留进程记录，请稍后再次 down')


async def wait_released_port(port: int) -> None:
    """受控停机容器重启前，最多等五秒让 Docker 释放监听端口。"""
    for attempt in range(51):
        try:
            require_free_port(port)
            return
        except RuntimeError:
            if attempt == 50:
                raise
        await asyncio.sleep(0.1)


async def docker(directory: Path, credentials: dict, port: int, *, start: bool) -> None:
    """管理专属容器及卷，保留数据库数据，不接管同名未知资源。"""
    if shutil.which('docker') is None:
        raise RuntimeError('未安装 Docker；需要可用的 Docker Postgres，不会降级到内存')
    code, _ = await command('docker', 'info', '--format', '{{.ServerVersion}}')
    if code:
        raise RuntimeError('Docker 未就绪；请启动 Docker 后重试')
    owner = credentials['owner']
    name, volume = 'xuanpu-local-' + owner, 'xuanpu-local-data-' + owner
    for kind, resource in [('volume', volume), ('container', name)]:
        code, output = await command('docker', kind, 'inspect', resource)
        if not code:
            info = json.loads(output)[0]
            labels = info.get('Labels') if kind == 'volume' else info['Config'].get('Labels')
            if (labels or {}).get(LABEL) != owner:
                raise RuntimeError('Docker 资源归属不符，拒绝操作')
        elif kind == 'volume' and start:
            code, _ = await command('docker', 'volume', 'create', '--label', f'{LABEL}={owner}', volume)
            if code:
                raise RuntimeError('无法创建持久卷')
        elif kind == 'container' and start:
            require_free_port(port)
            env = directory / 'postgres.env'
            descriptor = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write('POSTGRES_USER=xuanpu\nPOSTGRES_DB=xuanpu\nPOSTGRES_PASSWORD=' + credentials['postgres_password'] + '\n')
            code, _ = await command('docker', 'run', '-d', '--name', name, '--label', f'{LABEL}={owner}',
                '--env-file', str(env), '-p', f'127.0.0.1:{port}:5432', '-v', f'{volume}:/var/lib/postgresql/data',
                'postgres:16', timeout=180)
            if code:
                raise RuntimeError('无法启动 Postgres；请检查 Docker、镜像及端口')
    if not start:
        code, _ = await command('docker', 'container', 'inspect', name)
        if not code:
            code, _ = await command('docker', 'stop', '--time', '30', name, timeout=40)
            if code:
                raise RuntimeError('数据库停止失败；未删除持久卷')
        return
    code, output = await command('docker', 'container', 'inspect', name)
    info = json.loads(output)[0]
    expected = [{'HostIp': '127.0.0.1', 'HostPort': str(port)}]
    mounts = info.get('Mounts', [])
    if (info['Config'].get('Image') != 'postgres:16'
            or info.get('HostConfig', {}).get('PortBindings', {}).get('5432/tcp') != expected
            or not any(item.get('Name') == volume and item.get('Destination') == '/var/lib/postgresql/data'
                       for item in mounts)):
        raise RuntimeError('Docker 配置与本地部署不一致，拒绝连接')
    if not info['State']['Running']:
        await wait_released_port(port)
        code, _ = await command('docker', 'start', name)
        if code:
            raise RuntimeError('数据库启动失败')
    for _ in range(60):
        code, _ = await command('docker', 'exec', name, 'pg_isready', '-U', 'xuanpu', '-d', 'xuanpu')
        if code == 0:
            return
        await asyncio.sleep(0.5)
    raise RuntimeError('数据库未在 30 秒内就绪')


async def bootstrap(client: httpx.AsyncClient, directory: Path, credentials: dict,
                    data: dict, ticket_port: int) -> None:
    """首次建立 acme 管理凭据、保管库与样例包；重启只验证原凭据。"""
    if credentials.get('key'):
        client.headers['Authorization'] = 'Bearer ' + credentials['key']
        response = await client.get('/v1/me')
        response.raise_for_status()
        if response.json().get('tenant_id') != 'acme' or response.json().get('role') != 'tenant_admin':
            raise RuntimeError('保存的凭据不是 acme 管理员，拒绝继续')
        if credentials.get('bootstrap_complete'):
            return
    else:
        client.headers['Authorization'] = 'Bearer ' + credentials['root']
        response = await client.post('/v1/admin/tenants', json={'tenant_id': 'acme', 'name': '本地企业'})
        if response.status_code != 409:
            response.raise_for_status()
        response = await client.post('/v1/admin/tenants/acme/keys', json={'name': '本地管理员', 'role': 'tenant_admin'})
        response.raise_for_status()
        credentials['key'] = response.json()['key']
        write_private(directory / 'credentials.json', credentials)
        client.headers['Authorization'] = 'Bearer ' + credentials['key']
    feishu = data['feishu']
    values = {'tickets-token': credentials['tickets_token'], 'feishu-app-secret': feishu['app_secret']}
    for field, name in [('encrypt_key', 'feishu-encrypt-key'), ('verification_token', 'feishu-verification-token')]:
        if feishu[field]:
            values[name] = feishu[field]
    for name, value in values.items():
        (await client.put('/v1/secrets/' + name, json={'value': value})).raise_for_status()
    (await client.put('/v1/identity/feishu', json=feishu_payload(feishu))).raise_for_status()
    package = directory / 'support-desk'
    shutil.copytree(ROOT / 'examples/support-desk', package, dirs_exist_ok=True)
    path = package / 'xuanpu.yaml'
    manifest = yaml.safe_load(path.read_text())
    manifest['metadata']['version'] = f'1.0.0-local.{ticket_port}'
    manifest['spec']['connectors'][0]['baseUrl'] = f'http://127.0.0.1:{ticket_port}'
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True))
    archive = pack_package(package)
    response = await client.get('/v1/packages', params={'name': 'support-desk'})
    response.raise_for_status()
    version = manifest['metadata']['version']
    if any(item['version'] == version for item in response.json()['packages']):
        response = await client.post('/v1/packages/support-desk/activate', json={'version': version})
    else:
        response = await client.post('/v1/packages?activate=true', content=archive, headers={'Content-Type': 'application/zip'})
    response.raise_for_status()
    credentials['bootstrap_complete'] = True
    write_private(directory / 'credentials.json', credentials)


async def wait_ready(base: str, record: dict) -> None:
    """有界等待真实 CLI，就绪前持续核验进程身份。"""
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        for _ in range(120):
            if not await owned(record):
                raise RuntimeError('平台提前退出，请检查私有日志')
            try:
                if (await client.get(base + '/healthz')).is_success:
                    return
            except httpx.TransportError:
                pass  # 就绪探测只在固定期限内重试网络未监听。
            await asyncio.sleep(0.5)
    raise RuntimeError('平台 60 秒内未就绪')


async def up(args: argparse.Namespace, directory: Path) -> None:
    """先验证端口与已有实例，再启动数据库、迁移、工单与平台。"""
    data = validate_input(read_private(args.input))
    state_path = directory / 'processes.json'
    state = read_private(state_path) if state_path.exists() else {}
    living = []
    for record in state.values():
        if await identity(record['pid']) is not None:
            if not await owned(record):
                raise RuntimeError('记录进程归属不符；拒绝启动或停止任何进程')
            living.append(record)
    if living:
        saved = read_private(directory / 'credentials.json')
        fingerprint = hashlib.sha256(json.dumps([data, args.port, args.ticket_port, args.postgres_port], sort_keys=True).encode()).hexdigest()
        if len(living) == 2 and len(state) == 2 and saved['input_fingerprint'] == fingerprint:
            show_addresses(directory, args.port)
            return
        raise RuntimeError('已有部署进程运行；修改配置或恢复部分失败前请先 down')
    credentials = prepare(directory, data, args.port, args.ticket_port, args.postgres_port)
    require_free_port(args.port)
    require_free_port(args.ticket_port)
    cli = Path(sys.executable).parent / 'xuanpu'
    if not cli.is_file():
        raise RuntimeError('当前 Python 环境未安装 xuanpu CLI；请先 uv sync --all-packages')
    await docker(directory, credentials, args.postgres_port, start=True)
    code, _ = await command(str(cli), 'migrate', '--config', str(directory / 'config.json'), timeout=180)
    if code:
        raise RuntimeError('控制面迁移失败；保留数据库与配置，请检查数据库和 CLI 版本')
    worker = str(ROOT / 'scripts/local_trial_worker.py')
    write_private(directory / 'input.json', data)
    state = {}
    try:
        for name in ('tickets', 'platform'):
            state[name] = await spawn(directory, name, [sys.executable, worker, name, str(directory), str(args.ticket_port)])
            write_private(state_path, state)
        base = f'http://127.0.0.1:{args.port}'
        await wait_ready(base, state['platform'])
        async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=30) as client:
            await bootstrap(client, directory, credentials, data, args.ticket_port)
    except BaseException:
        for record in state.values():
            await stop(record)
        raise
    show_addresses(directory, args.port)


def show_addresses(directory: Path, port: int) -> None:
    """交付可访问地址与凭据位置，从不输出凭据值。"""
    base = f'http://127.0.0.1:{port}'
    print(f'控制台：{base}/console/\n飞书回调：{base}/v1/auth/feishu/callback\n事件地址：{base}/v1/identity/feishu/events/acme\n管理员凭据文件：{directory / "credentials.json"}')


async def run(args: argparse.Namespace, directory: Path) -> None:
    """分派生命周期操作；停止时保留所有数据和配置。"""
    if args.action == 'up':
        if args.input is None:
            raise ValueError('up 需要 --input 私有 JSON 文件路径')
        await up(args, directory)
        return
    state_path = directory / 'processes.json'
    state = read_private(state_path) if state_path.exists() else {}
    if args.action == 'status':
        for name, record in state.items():
            print(f'{name}: ' + ('运行中' if await owned(record) else '未运行或归属不符'))
        if not state:
            print('未记录本地部署进程')
        return
    for record in state.values():
        await stop(record)
    if (directory / 'credentials.json').exists():
        await docker(directory, read_private(directory / 'credentials.json'), args.postgres_port, start=False)
    write_private(state_path, {})
    print('本地服务已停止；配置、凭据与数据库卷均保留')


def main() -> None:
    """命令行入口只展示安全错误，不输出配置值或第三方错误正文。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['up', 'status', 'down'])
    parser.add_argument('--input', type=Path)
    parser.add_argument('--runtime', type=Path, default=Path.home() / '.local/share/xuanpu/local')
    parser.add_argument('--port', type=int, default=8420)
    parser.add_argument('--ticket-port', type=int, default=8431)
    parser.add_argument('--postgres-port', type=int, default=25432)
    args = parser.parse_args()
    try:
        # 仅规范化系统父目录；末级链接仍由 private_dir 拒绝。
        directory = args.runtime.parent.resolve() / args.runtime.name
        if directory.is_relative_to(ROOT):
            raise RuntimeError('运行目录不能放在代码仓库内；请使用默认私有目录')
        private_dir(directory)
        descriptor = os.open(directory / 'lifecycle.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(run(args, directory))
    except (RuntimeError, ValueError) as error:
        print(str(error) if type(error) is RuntimeError else '输入配置无效，请检查必要字段、地址、协议与事件密钥格式', file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print('部署操作失败；请检查权限、端口、Docker 及私有运行目录。秘密错误正文未输出。', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

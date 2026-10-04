"""本地服务守护入口：继承私有配置，过滤日志秘密，并转发终止信号。"""
from __future__ import annotations

import asyncio
import importlib.util
import signal
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

from local_trial_support import read_private

ROOT = Path(__file__).resolve().parents[1]


def secrets_in(directory: Path) -> list[str]:
    """仅取秘密字段用于日志替换，避免把配置写入输出。"""
    credentials = read_private(directory / 'credentials.json')
    data = read_private(directory / 'input.json')
    config = read_private(directory / 'config.json')
    return [value for value in [credentials.get('root'), credentials.get('key'),
        credentials.get('postgres_password'), credentials.get('tickets_token'), data['model']['api_key'],
        data['feishu']['app_secret'], data['feishu'].get('encrypt_key'),
        data['feishu'].get('verification_token'), *config['master_keys'].values()] if value]


async def platform(directory: Path) -> None:
    """监督真实 CLI 子进程，平台完成清理之后守护进程才退出。"""
    cli = Path(sys.executable).parent / 'xuanpu'
    process = await asyncio.create_subprocess_exec(str(cli), 'serve', '--config', str(directory / 'config.json'),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    loop = asyncio.get_running_loop()
    def terminate() -> None:
        """将终止信号转发给仍在运行的平台。"""
        if process.returncode is None:
            process.terminate()
    loop.add_signal_handler(signal.SIGTERM, terminate)
    loop.add_signal_handler(signal.SIGINT, terminate)
    if process.stdout is None:
        raise RuntimeError('平台输出管道不可用')
    try:
        async for line in process.stdout:
            output = line.decode(errors='replace')
            # 每行重新读凭据，以覆盖引导时新生成的管理员 key。
            for value in secrets_in(directory):
                output = output.replace(value, '[REDACTED]')
            print(output, end='', flush=True)
        await process.wait()
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
    raise SystemExit(process.returncode)


def tickets(directory: Path, port: int) -> None:
    """复用仓库工单样例，令牌来自私有文件，地址可配置。"""
    spec = importlib.util.spec_from_file_location('local_tickets', ROOT / 'examples/ticket-api/server.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('工单样例不可用')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TOKEN = read_private(directory / 'credentials.json')['tickets_token']
    ThreadingHTTPServer(('127.0.0.1', port), module.Handler).serve_forever()


if __name__ == '__main__':
    try:
        role, location, ticket_port = sys.argv[1:]
        if role == 'platform':
            asyncio.run(platform(Path(location)))
        else:
            tickets(Path(location), int(ticket_port))
    except Exception:
        print('本地服务启动失败，配置或依赖不可用；秘密错误正文未输出', file=sys.stderr)
        raise SystemExit(1) from None

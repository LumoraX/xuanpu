"""仅本机合成员工的浏览器验收夹具，无真实供应商或外部写入。"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import secrets
import sys
import time
from pathlib import Path

import uvicorn
from fastapi.responses import HTMLResponse, RedirectResponse
from xuanpu_console import static_dir
from xuanpu_server.api.app import create_app
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.identity.config import IdentityConfig
from xuanpu_server.identity.cookies import login_token_hash
from xuanpu_server.identity.models import LoginSession, UserRecord
from xuanpu_server.identity.service import IdentitySettings
from xuanpu_server.organization.config import OrganizationConfig
from xuanpu_server.shared_matters.models import SharedMatterConfig


async def serve(directory: Path, port: int) -> None:
    """使用真实应用与当前静态资源，合成会话仅绑定回环地址。"""
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('r3_browser_test_support', root / 'packages/xuanpu-server/tests/conftest.py')
    assert spec is not None and spec.loader is not None
    support = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = support
    spec.loader.exec_module(support)
    origin = 'http://127.0.0.1:' + str(port)
    settings = IdentitySettings.from_config(origin, IdentityConfig(cookie_secure=False))
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    world = await support.open_world(directory, store=InMemoryControlPlane(),
        sandboxes=support.local_sandboxes(), identity_settings=settings)
    platform = world.platform
    platform.organization.config = OrganizationConfig(enabled=True, tenants=('acme',))
    platform.shared_matters.config = SharedMatterConfig(enabled=True, tenants=('acme',))
    tokens = {}
    for name in ('alice', 'bob', 'outsider'):
        user, token, now = 'usr_' + name, secrets.token_urlsafe(32), time.time()
        await platform.identity.store.create_user(UserRecord('acme', user, '合成员工 ' + name, now))
        await platform.identity.store.create_login_session(LoginSession(login_token_hash(token), 'acme', user,
            now, now + 7200, now))
        tokens[name] = token
    world.models.script(*(support.Step(text=json.dumps({'message': '根据已选的固定材料核对：金额9876。'})) for _ in range(10)))
    app = create_app(platform, console_dir=static_dir())

    @app.get('/fixture')
    async def choices() -> HTMLResponse:
        """切换合成身份的可见入口，只存在于本机验收夹具。"""
        return HTMLResponse('<h1>本机合成验收</h1><p>当前为内存夹具，无真实员工及企业数据。</p>' +
            ''.join(f'<p><a href="/fixture/login/{name}">登录合成员工 {name}</a></p>' for name in tokens))

    @app.get('/fixture/login/{name}')
    async def login(name: str) -> RedirectResponse:
        """显式选择已预配合成员工；不是生产登录绕过功能。"""
        if name not in tokens:
            raise ValueError('未知合成人员')
        response = RedirectResponse('/console/', status_code=303)
        response.set_cookie('xuanpu_session', tokens[name], httponly=True, samesite='lax', path='/')
        return response

    try:
        await uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning')).serve()
    finally:
        await platform.close()


def main() -> None:
    """只接受新建合成目录及本机端口，不能通过参数连接生产。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8843)
    args = parser.parse_args()
    asyncio.run(serve(args.directory, args.port))


if __name__ == '__main__':
    main()

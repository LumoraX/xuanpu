"""合成浏览器Cookie与CSRF，机器客户端始终没有员工身份。"""
from __future__ import annotations

import time

import httpx
from xuanpu_server.api.app import create_app
from xuanpu_server.identity.cookies import login_token_hash
from xuanpu_server.identity.models import LoginSession, UserRecord


async def employee_client(platform, tenant: str, user: str, role: str):
    """创建测试员工本人登录，不提供生产模拟登录端点。"""
    now = time.time()
    await platform.identity.store.create_user(UserRecord(
        tenant_id=tenant, user_id=user, display_name=user, created_at=now, role=role))
    token = 'r4-synthetic-' + tenant + '-' + user
    await platform.identity.store.create_login_session(LoginSession(
        login_token_hash(token), tenant, user, now, now + 3600, now))
    cookies = httpx.Cookies()
    cookies.set('__Host-xuanpu_session', token, domain='office.test', path='/')
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(platform)),
        base_url='https://office.test', cookies=cookies,
        headers={'Origin': 'https://office.test', 'X-Requested-By': 'xuanpu'})

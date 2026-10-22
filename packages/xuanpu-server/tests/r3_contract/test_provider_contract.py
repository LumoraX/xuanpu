"""连接域必须存在且默认关闭，机器身份不能成为员工委托。"""
from __future__ import annotations

import httpx
from xuanpu_server.api.app import create_app


async def test_connection_domain_is_registered(world):
    """两个控制面后端必须提供独立的连接存储。"""
    assert hasattr(world.platform.store, 'connection_store')


async def test_disabled_connections_are_explicit(world):
    """未启用时明确拒绝，不能默默执行外部读取。"""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(world.platform)),
                                 base_url='http://test') as client:
        result = await client.get('/v1/me/connections', headers={
            'Authorization': 'Bearer ' + world.member_key})
    assert result.status_code == 403

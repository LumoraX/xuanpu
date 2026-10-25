"""R4真实双后端平台与明确合成员工，未验项不由夹具标记通过。"""
from __future__ import annotations

import time

import httpx
import pytest
from xuanpu_server.api.app import create_app
from xuanpu_server.identity.config import IdentityConfig
from xuanpu_server.identity.service import IdentitySettings
from xuanpu_server.tenancy import Tenant

from .client_helpers import employee_client
from .seed import delivery, package_bytes


@pytest.fixture
async def r4_world(identity_stores, world_opener, local_sandboxes):
    """生产默认关闭；测试服务存在后显式开启合成租户白名单。"""
    control, identity = identity_stores
    world = await world_opener(store=control, identity_store=identity,
        identity_settings=IdentitySettings.from_config('https://office.test', IdentityConfig()),
        sandboxes=local_sandboxes())
    if hasattr(world.platform, 'capabilities'):
        from xuanpu_server.capabilities.config import CapabilityDeliveryConfig
        world.platform.capabilities.config = CapabilityDeliveryConfig(enabled=True, tenants=('acme',))
    try:
        yield world
    finally:
        await world.platform.close()


@pytest.fixture
async def r4_http(r4_world):
    """不同员工与异租户采用不同的真实测试登录记录。"""
    platform = r4_world.platform
    await platform.store.create_tenant(Tenant('beta', '第二合成企业', created_at=time.time()))
    clients = {}
    for key, role in [('alice', 'tenant_member'), ('bob', 'tenant_member'), ('admin', 'tenant_admin')]:
        clients[key] = await employee_client(platform, 'acme', 'usr_' + key, role)
    clients['outsider'] = await employee_client(platform, 'beta', 'usr_admin', 'tenant_admin')
    clients['machine'] = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(platform)),
        base_url='https://office.test', headers={'Authorization': 'Bearer ' + r4_world.member_key})
    try:
        yield clients
    finally:
        for client in clients.values():
            await client.aclose()


@pytest.fixture
async def r4_ids(r4_world):
    """真实校验发布测试包，描述不进入包ZIP。"""
    package = await r4_world.platform.packages.publish(
        r4_world.admin, package_bytes(r4_world.package_dir / 'r4'), activate=True)
    return {'delivery': delivery(package.digest), 'alice_id': 'usr_alice', 'bob_id': 'usr_bob'}

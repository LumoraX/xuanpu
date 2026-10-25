"""管理视图、SDK和员工目录的真实HTTP合同。"""
from __future__ import annotations

import httpx
from xuanpu_sdk import XuanpuClient


async def test_sdk_register_preview_install_and_list(r4_world, r4_ids):
    """SDK不能跳过确认，也不把staged安装显示成可运行。"""
    assert hasattr(XuanpuClient, 'register_capability'), '尚未接入能力SDK'
    from xuanpu_server.api.app import create_app
    async with XuanpuClient(base_url='https://office.test', api_key=r4_world.admin_key,
        transport=httpx.ASGITransport(app=create_app(r4_world.platform))) as client:
        record = await client.register_capability({'request_id': 'sdk-register', 'delivery': r4_ids['delivery']})
        cap = record['capability_id']
        preview = await client.preview_capability_install(cap, {'request_id': 'sdk-preview', 'revision': 1})
        installed = await client.install_capability(cap, {'request_id': 'sdk-install',
            'preview_id': preview['id'], 'expected_version': 0, 'confirmed': True})
        listing = await client.list_capabilities(limit=1)
        assert listing['total'] == 1
        assert listing['installations'][0]['id'] == installed['id']
        assert listing['installations'][0]['status'] == 'staged'


async def test_catalog_totals_are_independent_of_page(r4_http, r4_ids):
    """旧修订保留且总数不随分页缩小，员工不能枚举管理目录。"""
    import copy
    client = r4_http['admin']
    for index in range(3):
        descriptor = copy.deepcopy(r4_ids['delivery'])
        descriptor['title'] = '修订' + str(index)
        response = await client.post('/v1/capabilities', json={'request_id': 'r-' + str(index), 'delivery': descriptor})
        assert response.status_code == 201, response.text
    response = await client.get('/v1/capabilities?limit=1')
    assert response.status_code == 200
    body = response.json()
    assert body['total'] == 3 and len(body['items']) == 1
    second = await client.get('/v1/capabilities', params={'limit': 1, 'cursor': body['next_cursor']})
    assert second.json()['total'] == 3
    assert second.json()['items'] != body['items']
    assert (await r4_http['alice'].get('/v1/capabilities')).status_code == 403
    assert (await r4_http['outsider'].get('/v1/capabilities')).json()['items'] == []
    assert (await client.get('/v1/capabilities?cursor=bad')).status_code == 400


async def test_cookie_write_requires_csrf(r4_http, r4_ids):
    """上传模板也不能绕过原生Cookie写保护。"""
    response = await r4_http['admin'].post('/v1/capabilities',
        headers={'Origin': 'https://attacker.test'}, json={'request_id': 'csrf', 'delivery': r4_ids['delivery']})
    assert response.status_code == 403

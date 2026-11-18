"""办公样板跨企业复制（R4-E1 一部分）：两个隔离合成企业安装同一归档与外部交付描述，
各自填本地配置、各自预检与安装；包摘要相同、安装配置摘要不同；任何跨企业的能力或安装标识都 404。
运行与成果的跨企业核对需办公容器，不在本测试内。内存与 PG。"""
from __future__ import annotations

import json
from pathlib import Path

from xuanpu_server.capabilities.config import CapabilityDeliveryConfig
from xuanpu_spec import pack_package

SAMPLE = Path(__file__).resolve().parents[4] / 'examples' / 'office-review'


async def _install(client, title: str) -> dict:
    """发布归档、登记描述、预检并安装；返回能力、预检与安装记录。"""
    archive = pack_package(SAMPLE / 'package')
    published = await client.post('/v1/packages', content=archive)
    assert published.status_code == 201, published.text
    delivery = json.loads((SAMPLE / 'delivery.json').read_text())
    registered = await client.post('/v1/capabilities', json={'request_id': 'office', 'delivery': delivery})
    assert registered.status_code == 201, registered.text
    cap = registered.json()['capability_id']
    preview = await client.post(f'/v1/capabilities/{cap}/install-previews', json={
        'request_id': 'preview', 'revision': 1,
        'public_config': {'report_title': title, 'group_column': '项目', 'amount_column': '金额'}})
    assert preview.status_code == 201, preview.text
    installed = await client.post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': 'install', 'preview_id': preview.json()['id'], 'expected_version': 0, 'confirmed': True})
    assert installed.status_code == 201, installed.text
    return {'capability': cap, 'published': published.json(), 'preview': preview.json(),
            'installation': installed.json()}


async def test_same_archive_installs_independently_in_two_companies(r4_http, r4_world):
    """同一归档摘要；各自的本地配置得到不同的配置摘要；跨企业读取、预检与停用都 404。"""
    r4_world.platform.capabilities.config = CapabilityDeliveryConfig(enabled=True, tenants=('acme', 'beta'))
    first = await _install(r4_http['admin'], '甲公司月度汇总')
    second = await _install(r4_http['outsider'], '乙公司费用复核')
    assert first['published']['digest'] == second['published']['digest']
    digests = []
    for tenant, record in (('acme', first), ('beta', second)):
        state, _ = await r4_world.platform.capabilities.store.capability_snapshot(tenant)
        installation = state.installations[record['installation']['id']]
        digests.append(state.configurations[installation.config_id].digest)
        assert record['installation']['id'] not in str((await r4_world.platform.capabilities.store.capability_snapshot(
            'beta' if tenant == 'acme' else 'acme'))[0].installations)
    assert digests[0] != digests[1], '各企业本地配置不同，配置摘要必须不同'
    assert first['capability'] != second['capability']
    outsider, admin = r4_http['outsider'], r4_http['admin']
    cap, installation = first['capability'], first['installation']['id']
    assert (await outsider.post(f'/v1/capabilities/{cap}/install-previews',
                                json={'request_id': 'steal', 'revision': 1})).status_code == 404
    assert (await outsider.post(f'/v1/installations/{installation}/state', json={
        'request_id': 'stop', 'expected_version': 1, 'status': 'disabled'})).status_code == 404
    assert (await outsider.get(f'/v1/capabilities/{cap}/releases')).status_code == 404
    assert (await outsider.get(f'/v1/capabilities/{cap}/assignments')).status_code == 404
    listed = (await admin.get('/v1/capabilities')).json()
    assert {i['capability_id'] for i in listed['items']} == {cap}
    assert second['capability'] not in json.dumps(listed) and '乙公司' not in json.dumps(listed)

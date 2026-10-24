"""公开SDK经真实ASGI/认证/存储验证固定副本和撤权。"""
from __future__ import annotations

import httpx
import pytest
from xuanpu_sdk import XuanpuClient, XuanpuError
from xuanpu_server.api.app import create_app


async def test_sdk_same_employee_shared_material_roundtrip(shared_http, r3_world):
    """不通过假下载或管理员越权替代实际员工权限链。"""
    platform = r3_world.platform
    key = await platform.issue_key(r3_world.admin, tenant_id='acme', name='合成SDK验收',
        role='tenant_member', act_as_users=True)
    transport = httpx.ASGITransport(app=create_app(platform))
    async with XuanpuClient('https://office.test', key.key, act_as_user='usr_alice', transport=transport) as alice, \
               XuanpuClient('https://office.test', key.key, act_as_user='usr_bob', transport=transport) as bob:
        created = await alice.create_shared_matter({'request_id': 'sdk-shared', 'confirmed': True, 'title': '明确固定副本'})
        identifier = created['id']
        publication = await alice.upload_shared_material(identifier, b'fixed-sdk-material', name='sdk.txt',
            mime_type='text/plain', request_id='sdk-upload', expected_version=1, confirmed=True)
        current = await alice.shared_publication(identifier, publication['id'])
        assert current['status'] == 'ready'
        material = publication['material_id']
        assert await alice.download_shared_material(identifier, material, revision=1) == b'fixed-sdk-material'
        with pytest.raises(XuanpuError) as denied:
            await bob.download_shared_material(identifier, material, revision=1)
        assert denied.value.status == 404
        await alice.invite_shared_member(identifier, {'request_id': 'sdk-bob', 'confirmed': True,
            'expected_version': 3, 'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer'})
        assert await bob.download_shared_material(identifier, material, revision=1) == b'fixed-sdk-material'
        await alice.withdraw_shared_material(identifier, material, {
            'request_id': 'sdk-withdraw', 'confirmed': True, 'expected_version': 4})
        with pytest.raises(XuanpuError) as revoked:
            await bob.download_shared_material(identifier, material, revision=1)
        assert revoked.value.status == 404

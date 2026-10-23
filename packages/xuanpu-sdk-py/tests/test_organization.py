"""组织SDK不得改写CAS、选中变更或调用身份。"""
from __future__ import annotations

import json

import httpx
from xuanpu_sdk import XuanpuClient


async def test_organization_requests_preserve_ids_and_selection():
    """真实HTTP边界验证路径编码、稳定请求和显式批次。"""
    calls = []
    def receive(request):
        """只记录投影后的合约，不接收凭据覆盖。"""
        calls.append(request)
        return httpx.Response(200, json={'items': [], 'total': 0, 'next_cursor': None})
    async with XuanpuClient('https://example.test', 'synthetic-key', act_as_user='employee',
                            transport=httpx.MockTransport(receive)) as client:
        assert hasattr(client, 'organization_me'), 'SDK缺少组织入口'
        await client.organization_me()
        await client.organization_directory(limit=1, cursor='cursor')
        await client.list_organization('units', limit=5)
        await client.get_organization('units', 'a/b')
        body = {'request_id': 'stable', 'name': '部门'}
        await client.create_organization('units', body)
        await client.create_organization('units', body)
        await client.change_organization('units', 'unit', {'request_id': 'change', 'expected_version': 1, 'name': '新部门'})
        await client.preview_organization_import({'request_id': 'preview', 'connection_id': 'connection'})
        await client.get_organization_import('preview/id')
        await client.apply_organization_import('preview', {'request_id': 'apply', 'expected_version': 1,
            'source_revision': 'revision', 'selected_changes': [{'change_id': 'selected', 'action': 'create'}]})
    assert calls[3].url.raw_path == b'/v1/admin/organization/units/a%2Fb'
    assert calls[4].content == calls[5].content
    assert json.loads(calls[-1].content)['selected_changes'] == [{'change_id': 'selected', 'action': 'create'}]
    assert all(r.headers['X-Xuanpu-Act-As'] == 'employee' for r in calls)

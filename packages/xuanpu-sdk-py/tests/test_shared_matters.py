"""共享SDK保留确认、CAS及身份边界，下载验证真实字节。"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from xuanpu_sdk import XuanpuClient, XuanpuError, XuanpuTransportError


async def test_shared_requests_keep_explicit_confirmation_versions_and_identity():
    """客户端不补确认、不改操作号，路径段编码而非拼接任意URL。"""
    calls = []
    def receive(request):
        """记录真实HTTP边界，不伪造业务成功证据。"""
        calls.append(request)
        return httpx.Response(200, json={'id': 'receipt'})
    async with XuanpuClient('https://example.test', 'synthetic-key', act_as_user='employee',
                            transport=httpx.MockTransport(receive)) as client:
        assert hasattr(client, 'create_shared_matter'), 'SDK缺少共享入口'
        body = {'request_id': 'stable', 'confirmed': False, 'title': '先核对'}
        await client.create_shared_matter(body)
        await client.create_shared_matter(body)
        await client.get_shared_matter('a/b')
        await client.invite_shared_member('matter', {'request_id': 'invite', 'confirmed': True,
            'expected_version': 3, 'subject_kind': 'user', 'subject_id': 'bob', 'role': 'viewer'})
        await client.create_shared_context({'request_id': 'source', 'confirmed': True,
            'expected_version': 4, 'shared_id': 'matter', 'kind': 'assistant_conversation'})
        await client.shared_source_reply('source', '核对资料', request_id='chat')
    assert calls[0].content == calls[1].content and json.loads(calls[0].content)['confirmed'] is False
    assert calls[2].url.raw_path == b'/v1/shared-matters/a%2Fb'
    assert json.loads(calls[3].content)['expected_version'] == 3
    assert json.loads(calls[-1].content) == {'shared_context_id': 'source', 'text': '核对资料', 'request_id': 'chat'}
    assert all(request.headers['X-Xuanpu-Act-As'] == 'employee' for request in calls)


async def test_shared_material_download_verifies_checksum_before_returning_bytes():
    """内容损坏时不把未经核验的共享成果交给调用者。"""
    good = b'fixed-material'
    digest = hashlib.sha256(good).hexdigest()
    bad = False
    def receive(request):
        """提供有确定SHA的固定内容或故障字节。"""
        return httpx.Response(200, content=b'corrupt' if bad else good,
            headers={'X-Content-SHA256': digest, 'Content-Type': 'text/plain'})
    async with XuanpuClient('https://example.test', 'synthetic-key', transport=httpx.MockTransport(receive)) as client:
        assert hasattr(client, 'download_shared_material'), 'SDK缺少固定材料读取'
        assert await client.download_shared_material('matter', 'material', revision=2) == good
        bad = True
        with pytest.raises(XuanpuTransportError):
            await client.download_shared_material('matter', 'material', revision=2)


async def test_stream_permission_failure_is_typed_instead_of_kernel_event():
    """外发前撤权是平台错误，不能按缺字段内核事件解析而丢失可理解提示。"""
    wire = 'event: error\ndata: {"error":{"status":403,"code":"shared_access_revoked","message":"请重新选择来源"}}\n\nevent: end\ndata: {}\n\n'
    def receive(request):
        """提供实际SSE格式的在途权限失败。"""
        return httpx.Response(200, content=wire, headers={'Content-Type': 'text/event-stream'})
    async with XuanpuClient('https://example.test', 'synthetic-key', transport=httpx.MockTransport(receive)) as client:
        with pytest.raises(XuanpuError) as caught:
            async for _ in client.sessions.send('session', '继续'):
                pass
        assert caught.value.code == 'shared_access_revoked'

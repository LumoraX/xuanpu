"""实际MCP工具集合及失败收尾，不调用业务工具。"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from .seed import delivery


def tool_hash(property_name):
    """预先确定的假CRM schema，不从被测库存函数生成期望。"""
    schema = {'type': 'object', 'properties': {property_name: {'type': 'string'}}}
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@pytest.fixture
async def mcp_delivery(r4_world):
    """固定进程外假CRM，缺省会话预算按平台上限显式核对。"""
    record = await r4_world.platform.packages.publish(r4_world.admin, r4_world.archive(approval='standard'))
    result = delivery(record.digest)
    result['package'].update(name='support-desk', entry='triage')
    result['permissions'].update(max_tokens=2000000, max_iterations=12, tools=[
        {'name': 'mcp__crm__search_customer', 'schema_sha256': tool_hash('name'), 'effect': 'read'},
        {'name': 'mcp__crm__update_ticket', 'schema_sha256': tool_hash('id'), 'effect': 'write'}])
    result['inputs'] = {'contract': 'capability_receipt', 'max_files': 0, 'mime_types': []}
    result['outputs'] = {'contract': 'capability_receipt', 'output_kind': 'capability_result'}
    result['requirements']['secret_refs'] = ['crm-token']
    return result


async def test_probe_records_complete_observed_inventory(r4_world, r4_http, mcp_delivery, monkeypatch):
    """白名单外的实际工具变化也使已确认预检失效。"""
    response = await r4_http['admin'].post('/v1/capabilities', json={
        'request_id': 'register', 'delivery': mcp_delivery})
    assert response.status_code == 201, response.text
    cap = response.json()['capability_id']
    body = {'request_id': 'probe', 'revision': 1, 'secret_refs': {'crm-token': 'crm-token'}}
    response = await r4_http['admin'].post(f'/v1/capabilities/{cap}/install-previews', json=body)
    assert response.status_code == 201, response.text
    assert response.json()['can_install'] is True, response.text
    async with r4_world.platform.coordination.transaction('acme') as tx:
        record = tx.state.capabilities.previews[response.json()['id']]
        assert 'observed_inventory' in record.evidence
    from xuanpu_server.runtime import mcp_connectors
    original = mcp_connectors._list_mcp_tools

    async def grown(connect):
        """保留允许工具，仅让远端新增一个白名单外工具。"""
        tools = await original(connect)
        tools['new-write'] = {'name': 'new-write', 'inputSchema': {'type': 'object'}}
        return tools

    monkeypatch.setattr(mcp_connectors, '_list_mcp_tools', grown)
    result = await r4_http['admin'].post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': 'install', 'preview_id': response.json()['id'], 'expected_version': 0, 'confirmed': True})
    assert result.status_code == 409, result.text
    assert r4_world.calls() == []
    assert list((r4_world.data_dir / 'capability-probes').iterdir()) == []


async def test_failed_probe_retains_cleanup_until_success(r4_world, r4_http, r4_ids, monkeypatch):
    """启动尚未交出句柄时的清理凭证不能被普通失败吞掉。"""
    from xuanpu_server.errors import RuntimeCleanupRequiredError
    inventory = r4_world.platform.capabilities.installations.preflight.inventory
    calls = []
    can_clean = asyncio.Event()

    async def cleanup():
        """第一次清理失败，只有第二次明确解除后才允许移除目录。"""
        calls.append('cleanup')
        if not can_clean.is_set():
            raise OSError('synthetic cleanup failure')

    async def broken(*args, **kwargs):
        """模拟启动已创建资源但还未返回句柄。"""
        raise RuntimeCleanupRequiredError('synthetic partial startup', cleanup)

    original = inventory.sandboxes.open
    monkeypatch.setattr(inventory.sandboxes, 'open', broken)
    response = await r4_http['admin'].post('/v1/capabilities', json={
        'request_id': 'register', 'delivery': r4_ids['delivery']})
    cap = response.json()['capability_id']
    response = await r4_http['admin'].post(f'/v1/capabilities/{cap}/install-previews',
        json={'request_id': 'failure', 'revision': 1})
    assert response.status_code == 503, response.text
    assert calls
    assert list((r4_world.data_dir / 'capability-probes').iterdir())
    can_clean.set()
    monkeypatch.setattr(inventory.sandboxes, 'open', original)
    await inventory.recover()
    assert list((r4_world.data_dir / 'capability-probes').iterdir()) == []


async def test_secret_rotation_serializes_with_install_commit(r4_world, r4_http, mcp_delivery, monkeypatch):
    """真实PG在最终校验与提交之间阻止秘密更新抢先提交。"""
    from xuanpu_server.controlplane.postgres import PostgresControlPlane
    from xuanpu_server.coordination import postgres

    if not isinstance(r4_world.platform.store, PostgresControlPlane):
        pytest.skip('本用例核对真实PG行锁，不以memory模拟')
    client = r4_http['admin']
    response = await client.post('/v1/capabilities', json={'request_id': 'register', 'delivery': mcp_delivery})
    assert response.status_code == 201, response.text
    cap = response.json()['capability_id']
    response = await client.post(f'/v1/capabilities/{cap}/install-previews', json={
        'request_id': 'preview', 'revision': 1, 'secret_refs': {'crm-token': 'crm-token'}})
    assert response.status_code == 201 and response.json()['can_install'], response.text
    reached, commit = asyncio.Event(), asyncio.Event()
    original = postgres.save_capabilities

    async def paused(conn, namespace, tenant, before, after):
        """只暂停真正的安装提交，不阻塞前面的只读事务。"""
        if after.installations != before.installations:
            reached.set()
            await commit.wait()
        await original(conn, namespace, tenant, before, after)

    monkeypatch.setattr(postgres, 'save_capabilities', paused)
    install = asyncio.create_task(client.post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': 'install', 'preview_id': response.json()['id'], 'expected_version': 0, 'confirmed': True}))
    await asyncio.wait_for(reached.wait(), 10)
    rotate = asyncio.create_task(r4_world.platform.vault.put('acme', 'crm-token', 'new-synthetic', actor='test'))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(rotate), .1)
    finally:
        commit.set()
        result = await install
        await rotate
    assert result.status_code == 201, result.text

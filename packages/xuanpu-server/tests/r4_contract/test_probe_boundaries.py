"""取消、冷恢复和连接密文版本边界的独立反例。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


async def test_cancelled_connector_close_retains_remaining_closers():
    """一次取消后显式重试必须继续回收当前及剩余连接。"""
    from xuanpu_server.runtime.connectors import ConnectorSet
    calls = []

    async def interrupted():
        """第一次取消，第二次明确完成。"""
        calls.append('first')
        if calls.count('first') == 1:
            raise asyncio.CancelledError

    async def remaining():
        """仍须回收尚未遍历的后续句柄。"""
        calls.append('second')

    connectors = ConnectorSet(tenant_id='acme', vault=None, audit=None, submissions=None, executor=None, strict_cleanup=True)
    connectors._mcp = [SimpleNamespace(close=interrupted), SimpleNamespace(close=remaining)]
    with pytest.raises(asyncio.CancelledError):
        await connectors.close()
    await connectors.close()
    assert calls == ['first', 'first', 'second']


async def test_local_unknown_probe_keeps_cold_recovery_evidence(tmp_path):
    """本机没有持久进程身份时不能把未知目录当作已清理。"""
    from xuanpu_server.capabilities.probe_resources import ProbeResources
    from xuanpu_server.errors import RuntimeCleanupRequiredError
    from xuanpu_server.runtime.sandbox import LocalSandboxProvider
    root = tmp_path / 'probe_unknown'
    (root / 'connectors').mkdir(parents=True)
    resources = ProbeResources(tmp_path, LocalSandboxProvider())
    with pytest.raises(RuntimeCleanupRequiredError):
        await resources.recover()
    assert root.exists()


async def test_connection_underlying_credential_changes_evidence(r4_world, r4_ids):
    """连接本身未改版本时，秘密缺失仍阻断，覆盖仍改变预检依据。"""
    from xuanpu_server.capabilities.delivery import Delivery
    from xuanpu_server.capabilities.schemas import PreviewInput
    service = r4_world.platform.capabilities.installations.preflight
    data = r4_ids['delivery']
    data['permissions']['connections'] = [{'connector': 'directory', 'kind': 'wecom', 'operations': ['self_profile.read']}]
    descriptor = Delivery.model_validate(data)
    async with service.connections.transaction('acme') as state:
        state.records['cnx_test'] = {'id': 'cnx_test', 'kind': 'installation', 'status': 'active',
            'version': 1, 'secret_ref': 'connection-key', 'operations': ['self_profile.read']}
    request = PreviewInput(request_id='local', revision=1, connection_refs={'directory': 'cnx_test'})
    _, absent = await service.local('acme', descriptor, request)
    assert 'connection_credential_missing:directory' in absent
    await r4_world.platform.vault.put('acme', 'connection-key', 'synthetic-first', actor='test')
    before, missing = await service.local('acme', descriptor, request)
    assert missing == ()
    await r4_world.platform.vault.put('acme', 'connection-key', 'synthetic-second', actor='test')
    after, missing = await service.local('acme', descriptor, request)
    assert missing == () and after != before


@pytest.mark.parametrize('failure', [OSError, asyncio.CancelledError])
async def test_remote_transport_retry_after_interrupted_close(failure):
    """真实凭据传输包装不能在底层关闭前提前标记成功。"""
    from xuanpu_server.runtime.remote_mcp import VaultAuthTransport
    calls = []

    class Transport:
        """只在首次关闭失败，验证真实包装的重试责任。"""
        async def aclose(self):
            """第二次明确完成。"""
            calls.append('close')
            if len(calls) == 1:
                raise failure

    transport = VaultAuthTransport(Transport(), url='https://synthetic.test/mcp',
        auth=None, tenant_id='acme', vault=None)
    with pytest.raises(failure):
        await transport.aclose()
    await asyncio.gather(transport.aclose(), transport.aclose())
    assert calls == ['close', 'close']

"""内部密文与同租户普通密钥隔离。"""
from __future__ import annotations

import pytest
from xuanpu_server.errors import PlatformError


async def test_scoped_cipher_rejects_cross_subject_and_revision(r3_world):
    """相同主密钥下AAD不同也不能挪用密文。"""
    vault=r3_world.platform.vault
    assert hasattr(vault,'seal_scoped')
    scope='connection\0install-a\0alice\0token\01'
    blob=vault.seal_scoped('acme',scope,'hidden-token')
    assert b'hidden-token' not in blob
    assert vault.open_scoped('acme',scope,blob)=='hidden-token'
    for tenant,name in [('other',scope),('acme',scope+'new'),('acme',scope.replace('alice','bob'))]:
        with pytest.raises(PlatformError):
            vault.open_scoped(tenant,name,blob)
    assert await vault.names('acme')==['crm-token']


async def test_connection_transaction_rolls_back(r3_world):
    """回执和密文任一出错都不留下部分变更。"""
    store=r3_world.platform.store.connection_store
    with pytest.raises(RuntimeError):
        async with store.transaction('acme') as state:
            state.records['test']={'kind':'fixture'}
            state.credentials['test']=b'ciphertext'
            raise RuntimeError('abort')
    async with store.transaction('acme') as state:
        assert 'test' not in state.records
        assert 'test' not in state.credentials

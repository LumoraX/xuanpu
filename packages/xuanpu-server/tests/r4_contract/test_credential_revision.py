"""密文版本绑定必须覆盖同名覆盖、删除重建及同秒写入。"""
from __future__ import annotations

import hashlib

import pytest


async def test_bound_credential_uses_one_versioned_record(r4_world, monkeypatch):
    """校验后不能另读一次Secret并取得已经换掉的值。"""
    from xuanpu_server.errors import ConflictError, SecretMissingError
    vault = r4_world.platform.vault
    assert hasattr(vault, 'get_bound'), '尚未实现绑定密文版本读取'
    control = r4_world.platform.store
    await vault.put('acme', 'revision-test', 'first-value', actor='test')
    old = await control.get_secret('acme', 'revision-test')
    digest = hashlib.sha256(old.ciphertext).hexdigest()
    original = control.get_secret
    calls = []

    async def race(tenant, name):
        """实际读取一次后轮换，返回刚读取的不可变记录。"""
        record = await original(tenant, name)
        calls.append(name)
        await vault.put(tenant, name, 'second-value', actor='test')
        return record

    monkeypatch.setattr(control, 'get_secret', race)
    assert await vault.get_bound('acme', 'revision-test', digest) == 'first-value'
    assert calls == ['revision-test']
    monkeypatch.setattr(control, 'get_secret', original)
    with pytest.raises(ConflictError):
        await vault.get_bound('acme', 'revision-test', digest)
    await vault.delete('acme', 'revision-test')
    with pytest.raises(SecretMissingError):
        await vault.get_bound('acme', 'revision-test', digest)
    await vault.put('acme', 'revision-test', 'first-value', actor='test')
    with pytest.raises(ConflictError):
        await vault.get_bound('acme', 'revision-test', digest)

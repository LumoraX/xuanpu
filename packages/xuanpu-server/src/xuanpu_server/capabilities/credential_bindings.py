"""内部密文变化指纹与一次读取解密，不缓存明文到交付记录。"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, SecretMissingError

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import SecretRecord
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.secrets import SecretVault


def record_digest(record: SecretRecord) -> str:
    """随机化密文摘要不等于秘密明文摘要，且覆盖同秒重复更新。"""
    return hashlib.sha256(record.ciphertext).hexdigest()


async def bindings(control: ControlPlaneStore, tenant: str,
                   references: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """只读取租户内密文元数据，不为预检解出秘密值。"""
    versions = {}
    missing = []
    for slot, name in sorted(references.items()):
        record = await control.get_secret(tenant, name)
        if record is None:
            missing.append('secret_missing:' + slot)
        else:
            versions[slot] = record_digest(record)
    return versions, tuple(missing)


class BoundVault:
    """连接器预检使用精确记录，不允许同名换值扩大此前许可。"""
    def __init__(self, inner: SecretVault, references: dict[str, str], versions: dict[str, str]) -> None:
        """映射只存在服务端，不进入工具描述或公共预检视图。"""
        self.inner, self.references, self.versions = inner, references, versions

    async def get(self, tenant_id: str, name: str) -> str:
        """绑定读取能力未实现的外部Vault不能静默退回普通get。"""
        getter = getattr(self.inner, 'get_bound', None)
        if getter is None:
            raise ConflictError('当前密钥库尚不支持版本绑定读取')
        if name not in self.references or name not in self.versions:
            raise SecretMissingError('连接器的秘密引用尚未配置')
        result: str = await getter(tenant_id, self.references[name], self.versions[name])
        return result

    async def names(self, tenant_id: str) -> list[str]:
        """预检不会枚举保管库其他秘密。"""
        return sorted(self.references)

    async def put(self, tenant_id: str, name: str, value: str, *, actor: str) -> None:
        """预检密钥视图禁止写入。"""
        raise ConflictError('预检不能写入密钥')

    async def delete(self, tenant_id: str, name: str) -> bool:
        """预检密钥视图禁止删除。"""
        raise ConflictError('预检不能删除密钥')

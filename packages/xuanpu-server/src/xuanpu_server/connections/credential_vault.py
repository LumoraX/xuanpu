"""内部凭据复用平台加密组件，以完整作用域绑定AAD。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from xuanpu_server.errors import RuntimeUnavailableError

if TYPE_CHECKING:
    from xuanpu_server.secrets import SecretVault


@runtime_checkable
class ScopedCipher(Protocol):
    """现有加密保管库或KMS适配器提供的内部封装能力。"""
    def seal_scoped(self,tenant_id: str,scope: str,value: str) -> bytes:
        """使用当前主密钥写入。"""
        ...

    def open_scoped(self,tenant_id: str,scope: str,blob: bytes) -> str:
        """按原始作用域及密文主密钥标识解开。"""
        ...


def cipher(vault: SecretVault) -> ScopedCipher:
    """不具备内部加密能力时明确失败，不退回明文持久化。"""
    if not isinstance(vault,ScopedCipher):
        raise RuntimeUnavailableError('保管库尚未配置内部作用域加密能力')
    return vault


def scope(installation: str,subject: str,kind: str,revision: str) -> str:
    """租户由cipher单独绑定，其他维度不可互换。"""
    return '\0'.join(('connection',installation,subject,kind,revision))

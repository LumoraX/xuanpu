"""租户密钥的保管。

业务包与策略里只写密钥的名字（ADR 0004 决策 5）。明文只在两处出现：租户管理员写入时，
以及效应面出口把它注入目标进程时。明文不进提示词、不进日志、不进审计正文。

``SecretVault`` 是接缝：生产部署按 ADR 0003 接 Vault 或云 KMS；``EncryptedVault`` 是随平台
提供的默认实现，把密文存在控制面存储里，主密钥由部署方经配置注入。
"""

from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from xuanpu_server.controlplane.models import SecretRecord
from xuanpu_server.errors import InvalidRequestError, PlatformError, SecretMissingError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from xuanpu_server.controlplane.store import ControlPlaneStore

_SECRET_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_FORMAT = b"v1"
_NONCE_BYTES = 12
_KEY_BYTES = 32

MAX_SECRET_BYTES = 64 * 1024


class SecretCorruptedError(PlatformError):
    """密文无法解开：被改动过、被挪到了别的租户或名字下，或加密它的主密钥已不在配置里。"""

    code = "secret_corrupted"
    status = 500


@runtime_checkable
class SecretVault(Protocol):
    """密钥保管库。所有方法都限定在一个租户之内。"""

    async def put(self, tenant_id: str, name: str, value: str, *, actor: str) -> None:
        """写入或覆盖。"""
        ...

    async def get(self, tenant_id: str, name: str) -> str:
        """取明文；不存在抛 ``SecretMissingError``。"""
        ...

    async def names(self, tenant_id: str) -> list[str]:
        """列出名字（不返回取值）。"""
        ...

    async def delete(self, tenant_id: str, name: str) -> bool:
        """删除；返回是否确实删掉了。"""
        ...


def validate_secret_name(name: str) -> str:
    """密钥名与业务包清单里引用的写法一致。"""
    if not _SECRET_NAME.match(name):
        raise InvalidRequestError(
            f"密钥名非法：{name!r}（小写字母开头，只含小写字母、数字、连字符，最长 63）"
        )
    return name


def generate_master_key() -> bytes:
    """生成一把主密钥。"""
    return AESGCM.generate_key(bit_length=_KEY_BYTES * 8)


class EncryptedVault:
    """把密文存在控制面存储里的保管库。

    加密用 AES-256-GCM，附加认证数据是「租户 + 密钥名」：密文被挪到别的租户或别的名字下
    就解不开，数据库里的行不能被拿来移花接木。

    支持主密钥轮换：配置里同时放新旧两把，新写入用 ``primary``，读取按密文里记的标识
    选钥匙；旧密文全部重写后再撤掉旧钥匙。
    """

    def __init__(
        self,
        store: ControlPlaneStore,
        *,
        keys: Mapping[str, bytes],
        primary: str,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 控制面存储。
            keys: 主密钥，标识 → 32 字节密钥。
            primary: 新写入使用的主密钥标识。
            now: 取当前时间的函数，测试时可替换。
        """
        if primary not in keys:
            raise ValueError(f"主密钥 {primary!r} 不在 keys 里")
        for key_id, key in keys.items():
            if not _KEY_ID.match(key_id):
                raise ValueError(f"主密钥标识非法：{key_id!r}")
            if len(key) != _KEY_BYTES:
                raise ValueError(f"主密钥 {key_id!r} 必须是 {_KEY_BYTES} 字节")
        self._store = store
        self._ciphers = {key_id: AESGCM(key) for key_id, key in keys.items()}
        self._primary = primary
        self._now = now

    @staticmethod
    def _binding(tenant_id: str, name: str) -> bytes:
        """附加认证数据：把密文钉在它所属的租户与名字上。"""
        return f"{tenant_id}\x00{name}".encode()

    def _seal(self, tenant_id: str, name: str, value: str) -> bytes:
        """加密。格式：``v1:<主密钥标识>:<随机数><密文>``。"""
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._ciphers[self._primary].encrypt(
            nonce, value.encode("utf-8"), self._binding(tenant_id, name)
        )
        return b":".join((_FORMAT, self._primary.encode("ascii"), nonce + sealed))

    def _open(self, tenant_id: str, name: str, blob: bytes) -> str:
        """解密。"""
        parts = blob.split(b":", 2)
        if len(parts) != 3 or parts[0] != _FORMAT or len(parts[2]) <= _NONCE_BYTES:
            raise SecretCorruptedError(f"密钥 {name!r} 的密文格式无法识别")
        cipher = self._ciphers.get(parts[1].decode("ascii", errors="replace"))
        if cipher is None:
            raise SecretCorruptedError(f"密钥 {name!r} 是用一把已不在配置里的主密钥加密的")
        nonce, sealed = parts[2][:_NONCE_BYTES], parts[2][_NONCE_BYTES:]
        try:
            return cipher.decrypt(nonce, sealed, self._binding(tenant_id, name)).decode("utf-8")
        except InvalidTag as exc:
            raise SecretCorruptedError(f"密钥 {name!r} 的密文校验失败") from exc

    def seal_scoped(self, tenant_id: str, scope: str, value: str) -> bytes:
        """复用主密钥与轮换格式，为内部域的完整AAD作用域封装密文。"""
        return self._seal(tenant_id, scope, value)

    def open_scoped(self, tenant_id: str, scope: str, blob: bytes) -> str:
        """内部域必须提供原始作用域，跨员工或修订挪用会校验失败。"""
        return self._open(tenant_id, scope, blob)

    async def put(self, tenant_id: str, name: str, value: str, *, actor: str) -> None:
        """写入或覆盖。"""
        validate_secret_name(name)
        if not value:
            raise InvalidRequestError("密钥取值不能为空")
        if len(value.encode("utf-8")) > MAX_SECRET_BYTES:
            raise InvalidRequestError(f"密钥取值超过 {MAX_SECRET_BYTES} 字节")
        await self._store.put_secret(
            SecretRecord(
                tenant_id=tenant_id,
                name=name,
                ciphertext=self._seal(tenant_id, name, value),
                updated_at=self._now(),
                updated_by=actor,
            )
        )

    async def get(self, tenant_id: str, name: str) -> str:
        """取明文。"""
        record = await self._store.get_secret(tenant_id, name)
        if record is None:
            raise SecretMissingError(f"密钥 {name!r} 尚未配置")
        return self._open(tenant_id, name, record.ciphertext)

    async def get_bound(self, tenant_id: str, name: str, expected_record_digest: str) -> str:
        """从同一记录校验密文版本再解密，避免校验后另读已换值的秘密。"""
        import hashlib
        import hmac

        from xuanpu_server.errors import ConflictError

        record = await self._store.get_secret(tenant_id, name)
        if record is None:
            raise SecretMissingError('绑定的密钥尚未配置')
        actual = hashlib.sha256(record.ciphertext).hexdigest()
        if not hmac.compare_digest(actual, expected_record_digest):
            raise ConflictError('密钥记录已变化，请重新预检安装配置')
        return self._open(tenant_id, name, record.ciphertext)

    async def names(self, tenant_id: str) -> list[str]:
        """列出名字。"""
        return await self._store.list_secret_names(tenant_id)

    async def delete(self, tenant_id: str, name: str) -> bool:
        """删除。"""
        return await self._store.delete_secret(tenant_id, name)


__all__ = [
    "MAX_SECRET_BYTES",
    "EncryptedVault",
    "SecretCorruptedError",
    "SecretVault",
    "generate_master_key",
    "validate_secret_name",
]

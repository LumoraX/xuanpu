"""使用成熟 AES-GCM 的独立财务原件加密对象适配。"""

from __future__ import annotations

import hashlib
import secrets
import tempfile
from functools import partial
from pathlib import Path
from typing import Any, Protocol

import anyio
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from xuanpu_finance.errors import FinanceError


class OriginalObjects(Protocol):
    """只暴露原字节读写的对象存储协议。"""

    async def put(self, key: str, content: bytes) -> None:
        """加密保留原字节。"""
        ...

    async def get(self, key: str) -> bytes:
        """验证完整性并恢复原字节。"""
        ...


class _Envelope:
    """使用随机 nonce 和对象键关联数据的 AES-GCM 封装。"""

    def __init__(self, key: bytes) -> None:
        """要求显式注入 256 位原始密钥。"""
        if len(key) != 32:
            raise ValueError("原件密钥必须为 32 字节 AES-256 密钥")
        self.cipher = AESGCM(key)

    def validate(self, key: str) -> None:
        """拒绝绝对路径、空段和目录穿越对象键。"""
        if (
            not key
            or len(key) > 1024
            or any(part in {"", ".", ".."} for part in key.split("/"))
            or "\\" in key
        ):
            raise FinanceError("OBJECT_KEY_INVALID", 422, "对象键无效")

    def encrypt(self, key: str, content: bytes) -> bytes:
        """随机 nonce，绑定租户化对象键为关联数据。"""
        self.validate(key)
        nonce = secrets.token_bytes(12)
        return b"XF01" + nonce + self.cipher.encrypt(nonce, content, key.encode())

    def decrypt(self, key: str, encrypted: bytes) -> bytes:
        """认证失败显式报错，不返回部分明文。"""
        self.validate(key)
        if len(encrypted) < 32 or encrypted[:4] != b"XF01":
            raise FinanceError("OBJECT_CORRUPT", 500, "原件密文头损坏")
        try:
            return self.cipher.decrypt(encrypted[4:16], encrypted[16:], key.encode())
        except InvalidTag as exc:
            raise FinanceError("OBJECT_CORRUPT", 500, "原件认证失败") from exc


class EncryptedLocalObjects:
    """异步文件 IO、加密认证和原子替换的独立原件适配。"""

    def __init__(self, root: Path | str, key: bytes) -> None:
        """注入独立原件目录与原始 32 字节密钥，不读取环境。"""
        self.root = Path(root)
        self.envelope = _Envelope(key)

    def _path(self, key: str) -> Path:
        """只将验证后的对象键摘要用于磁盘路径。"""
        self.envelope.validate(key)
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.root / digest[:2] / f"{digest}.enc"

    def _write(self, path: Path, key: str, content: bytes, encrypted: bytes) -> None:
        """通过原子硬链接创建对象，已有对象只允许原字节完全相同。"""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(encrypted)
                stream.flush()
                stream.close()
                try:
                    path.hardlink_to(temporary)
                except FileExistsError:
                    if self.envelope.decrypt(key, path.read_bytes()) != content:
                        raise FinanceError(
                            "OBJECT_IMMUTABLE", 409, "原件对象键已绑定不同内容"
                        ) from None
            finally:
                temporary.unlink(missing_ok=True)

    async def put(self, key: str, content: bytes) -> None:
        """在线程池加密写入原件，不阻塞事件循环。"""
        path = self._path(key)
        encrypted = await anyio.to_thread.run_sync(self.envelope.encrypt, key, content)
        await anyio.to_thread.run_sync(self._write, path, key, content, encrypted)

    async def get(self, key: str) -> bytes:
        """读取并校验原件完整性。"""
        try:
            encrypted = await anyio.to_thread.run_sync(self._path(key).read_bytes)
        except FileNotFoundError as exc:
            raise FinanceError("OBJECT_NOT_FOUND", 404, "加密原件尚未写入或已缺失") from exc
        return await anyio.to_thread.run_sync(self.envelope.decrypt, key, encrypted)


class EncryptedS3Objects:
    """显式 S3 客户端和密钥注入的客户端加密对象适配。"""

    def __init__(self, client: Any, bucket: str, key: bytes, prefix: str = "finance") -> None:
        """客户端的端点、凭据和服务端策略由调用方管理。"""
        self.client, self.bucket, self.prefix = client, bucket, prefix
        self.envelope = _Envelope(key)
        self.envelope.validate(prefix)

    def _key(self, key: str) -> str:
        """隔离对象键到显式财务前缀并隐藏标识原文。"""
        self.envelope.validate(key)
        return self.prefix + "/" + hashlib.sha256(key.encode()).hexdigest() + ".enc"

    async def put(self, key: str, content: bytes) -> None:
        """先客户端认证加密，再将密文写入 S3 兼容服务。"""
        encrypted = await anyio.to_thread.run_sync(self.envelope.encrypt, key, content)
        try:
            await anyio.to_thread.run_sync(
                partial(
                    self.client.put_object,
                    Bucket=self.bucket,
                    Key=self._key(key),
                    Body=encrypted,
                    ContentType="application/octet-stream",
                    IfNoneMatch="*",
                )
            )
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 412:
                raise
            if await self.get(key) != content:
                raise FinanceError("OBJECT_IMMUTABLE", 409, "原件对象键已绑定不同内容") from exc

    async def get(self, key: str) -> bytes:
        """在线程池下载密文、关闭响应流并认证解密。"""
        try:
            response = await anyio.to_thread.run_sync(
                partial(self.client.get_object, Bucket=self.bucket, Key=self._key(key))
            )
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                raise FinanceError("OBJECT_NOT_FOUND", 404, "加密原件尚未写入或已缺失") from exc
            raise
        body = response["Body"]
        try:
            encrypted = await anyio.to_thread.run_sync(body.read)
        finally:
            await anyio.to_thread.run_sync(body.close)
        return await anyio.to_thread.run_sync(self.envelope.decrypt, key, encrypted)

"""S3 兼容的对象存储（阿里云 OSS 等）。需要可选依赖 ``xuanpu-server[s3]``。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from xuanpu_server.config import ConfigError
from xuanpu_server.objectstore.base import ObjectNotFoundError, validate_key

if TYPE_CHECKING:
    from botocore.exceptions import ClientError

_MISSING_CODES = {"NoSuchKey", "404", "NotFound"}


@dataclass(frozen=True)
class S3Settings:
    """连接一个 S3 兼容桶所需的设置。

    ``addressing_style`` 默认 ``virtual``（虚拟主机风格）：阿里云 OSS 只接受这种寻址；
    本机的 moto、MinIO 等通常要用 ``path``。两个密钥不进 ``repr``。
    """

    endpoint: str
    bucket: str
    region: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    prefix: str = ""
    addressing_style: Literal["virtual", "path"] = "virtual"


def _is_missing(error: ClientError) -> bool:
    """这个客户端错误是不是「对象不存在」。"""
    return str(error.response.get("Error", {}).get("Code", "")) in _MISSING_CODES


class S3ObjectStore:
    """经 boto3 访问 S3 兼容桶；所有键都加上 ``prefix``。阻塞调用放进线程。"""

    def __init__(self, settings: S3Settings) -> None:
        """建客户端（不发请求）。

        Raises:
            ConfigError: 没安装 boto3。
        """
        try:
            import boto3.session
            from botocore.config import Config
        except ImportError as exc:
            raise ConfigError("object_store.backend 为 s3 需要安装 xuanpu-server[s3]") from exc
        self._bucket = settings.bucket
        self._prefix = settings.prefix
        session = boto3.session.Session(
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            region_name=settings.region,
        )
        self._client: Any = session.client(
            "s3",
            endpoint_url=settings.endpoint,
            config=Config(
                s3={"addressing_style": settings.addressing_style},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def _full_key(self, key: str) -> str:
        """校验键并加上前缀。"""
        return self._prefix + validate_key(key)

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        """写入（覆盖）一个对象。"""
        full = self._full_key(key)
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=full,
            Body=data,
            ContentType=content_type,
        )

    async def get(self, key: str) -> bytes:
        """读出整个对象。不存在抛 ``ObjectNotFoundError``，其他错误原样上抛。"""
        full = self._full_key(key)
        try:
            return await asyncio.to_thread(self._read, full)
        except self._client_error() as exc:
            if _is_missing(exc):
                raise ObjectNotFoundError(f"对象不存在：{key}") from exc
            raise

    def _read(self, full: str) -> bytes:
        """取对象并读出全部内容。"""
        response = self._client.get_object(Bucket=self._bucket, Key=full)
        body: bytes = response["Body"].read()
        return body

    async def exists(self, key: str) -> bool:
        """对象是否存在（``head_object``）；404 返回 False，其他错误原样上抛。"""
        full = self._full_key(key)
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self._bucket, Key=full)
        except self._client_error() as exc:
            if _is_missing(exc):
                return False
            raise
        return True

    async def delete(self, key: str) -> None:
        """删除对象；S3 对不存在的键同样返回成功。"""
        full = self._full_key(key)
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=full)

    @staticmethod
    def _client_error() -> type[ClientError]:
        """botocore 的 ``ClientError``（延迟引入，因为 boto3 是可选依赖）。"""
        from botocore.exceptions import ClientError

        return ClientError


__all__ = ["S3ObjectStore", "S3Settings"]

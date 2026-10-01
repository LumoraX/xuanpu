"""S3 兼容的对象存储（阿里云 OSS 等）。需要可选依赖 ``xuanpu-server[s3]``。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from xuanpu_server.config import ConfigError
from xuanpu_server.objectstore.base import ObjectNotFoundError, validate_key

if TYPE_CHECKING:
    from botocore.exceptions import ClientError
    from mypy_boto3_s3 import S3Client

_MISSING_CODES = {"NoSuchKey", "404", "NotFound"}
_CONNECT_TIMEOUT_SECONDS = 10
"""建立连接的超时：对象存储在同地域内网，连不上说明配置或网络有问题，尽快报出来。"""
_READ_TIMEOUT_SECONDS = 60
"""读响应的超时：业务包归档与文件是整对象读写，给得比连接宽松。"""
_MAX_POOL_CONNECTIONS = 32
"""连接池大小：每个调用占一个线程（``to_thread``），池与默认线程池同量级以免排队。"""


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
    """经 boto3 访问 S3 兼容桶；所有键都加上 ``prefix``。阻塞调用放进线程。

    boto3 自身会读取标准的 ``AWS_*`` 与 ``HTTP(S)_PROXY`` 环境变量。
    """

    def __init__(self, settings: S3Settings) -> None:
        """建客户端（不发请求）。

        Raises:
            ConfigError: 没安装 boto3，或 boto3 拒绝这些设置（如 endpoint 不合法）。
        """
        try:
            import boto3.session
            from botocore.config import Config
            from botocore.exceptions import ClientError
        except ImportError as exc:
            raise ConfigError("object_store.backend 为 s3 需要安装 xuanpu-server[s3]") from exc
        self._client_error: type[ClientError] = ClientError
        self._bucket = settings.bucket
        self._prefix = settings.prefix
        session = boto3.session.Session(
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            region_name=settings.region,
        )
        try:
            self._client: S3Client = session.client(
                "s3",
                endpoint_url=settings.endpoint,
                config=Config(
                    s3={"addressing_style": settings.addressing_style},
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                    read_timeout=_READ_TIMEOUT_SECONDS,
                    max_pool_connections=_MAX_POOL_CONNECTIONS,
                    # botocore 1.36 起默认在 HTTPS 下给 put_object 加校验和并改用
                    # aws-chunked 分块编码（带 X-Amz-Trailer）。阿里云 OSS 不支持，
                    # 会报 InvalidArgument；只在服务端要求时才计算校验和。
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        except ValueError as exc:
            raise ConfigError(f"S3 设置不合法：{exc}") from exc

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
        """读出整个对象。不存在抛 ``ObjectNotFoundError``，其他错误（含桶不存在）原样上抛。"""
        full = self._full_key(key)
        try:
            return await asyncio.to_thread(self._read, full)
        except self._client_error as exc:
            if _is_missing(exc):
                raise ObjectNotFoundError(f"对象不存在：{key}") from exc
            raise

    def _read(self, full: str) -> bytes:
        """取对象并读出全部内容。"""
        response = self._client.get_object(Bucket=self._bucket, Key=full)
        return response["Body"].read()

    async def exists(self, key: str) -> bool:
        """对象是否存在（``head_object``）；404 返回 False，其他错误原样上抛。

        注意：桶不存在时 HEAD 同样回 404，所以这里会返回 False；桶名配错会在第一次
        ``put`` 时报出来。
        """
        full = self._full_key(key)
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self._bucket, Key=full)
        except self._client_error as exc:
            if _is_missing(exc):
                return False
            raise
        return True

    async def delete(self, key: str) -> None:
        """删除对象；S3 对不存在的键同样返回成功。"""
        full = self._full_key(key)
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=full)


__all__ = ["S3ObjectStore", "S3Settings"]

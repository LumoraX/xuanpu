"""对象存储：本机目录与 S3 兼容两种实现共用一组用例。"""

from __future__ import annotations

import sys
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.config import ConfigError
from xuanpu_server.objectstore import (
    LocalObjectStore,
    ObjectNotFoundError,
    ObjectStore,
    S3ObjectStore,
    S3Settings,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

BUCKET = "xuanpu-test"
_PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


def _settings(endpoint: str, bucket: str = BUCKET, *, style: str = "path") -> S3Settings:
    """S3 设置；默认指向本机 moto 的路径风格。"""
    return S3Settings(
        endpoint=endpoint,
        bucket=bucket,
        region="us-east-1",
        access_key_id="test",
        secret_access_key="test",
        prefix="p0/",
        addressing_style="path" if style == "path" else "virtual",
    )


def _raw_client(endpoint: str) -> Any:
    """测试里直接操作桶用的 boto3 客户端（建桶、列桶不是产品代码的职责）。"""
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture(scope="module")
def moto_endpoint() -> Iterator[str]:
    """整个模块只起一次 moto 的 S3 服务；moto 请求不能走开发机的代理。"""
    from moto.server import ThreadedMotoServer

    patch = pytest.MonkeyPatch()
    for name in _PROXY_VARS:
        patch.delenv(name, raising=False)
    patch.setenv("NO_PROXY", "127.0.0.1,localhost")
    patch.setenv("no_proxy", "127.0.0.1,localhost")
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.stop()
        patch.undo()


@pytest.fixture
def s3_server(moto_endpoint: str) -> S3Settings:
    """连 moto 的设置；每个用例一个独立的桶，互不依赖。"""
    bucket = f"xuanpu-{uuid.uuid4().hex[:12]}"
    _raw_client(moto_endpoint).create_bucket(Bucket=bucket)
    return _settings(moto_endpoint, bucket)


@pytest.fixture(params=["local", "s3"])
async def objects(
    request: pytest.FixtureRequest, tmp_path: Path, moto_endpoint: str
) -> ObjectStore:
    """本机目录实现，与 moto 的 S3 服务上的 S3 实现。"""
    if request.param == "local":
        return LocalObjectStore(tmp_path / "objects")
    bucket = f"xuanpu-{uuid.uuid4().hex[:12]}"
    _raw_client(moto_endpoint).create_bucket(Bucket=bucket)
    return S3ObjectStore(_settings(moto_endpoint, bucket))


class _FakeRaw:
    """假的响应体：给 AWSResponse 读内容用。"""

    def stream(self, *_: object, **__: object) -> Iterator[bytes]:
        """没有内容。"""
        return iter(())


def _intercept(store: S3ObjectStore, operation: str, status: int) -> list[Any]:
    """在发送前截下某个操作的请求，回一个假响应；返回收集到的请求列表。"""
    from botocore.awsrequest import AWSResponse

    seen: list[Any] = []

    def hook(request: Any, **_: object) -> AWSResponse:
        seen.append(request)
        return AWSResponse(request.url, status, {}, _FakeRaw())

    store._client.meta.events.register(f"before-send.s3.{operation}", hook)
    return seen


async def test_round_trip(objects: ObjectStore) -> None:
    """写、读、覆盖、删除、重复删除（不存在不算错）。"""
    await objects.put("tenants/acme/a.bin", b"\x00\x01")
    assert await objects.exists("tenants/acme/a.bin")
    assert await objects.get("tenants/acme/a.bin") == b"\x00\x01"
    await objects.put("tenants/acme/a.bin", b"v2")
    assert await objects.get("tenants/acme/a.bin") == b"v2"
    await objects.delete("tenants/acme/a.bin")
    await objects.delete("tenants/acme/a.bin")
    assert not await objects.exists("tenants/acme/a.bin")


async def test_missing_object(objects: ObjectStore) -> None:
    """读不存在的对象抛 ObjectNotFoundError，exists 为 False。"""
    with pytest.raises(ObjectNotFoundError) as caught:
        await objects.get("tenants/acme/nope")
    assert isinstance(caught.value, LookupError)
    assert not await objects.exists("tenants/acme/nope")


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "a//b", "a\\b", "a/./b", "a\x00b", "a\nb", "a/.xuanpu-tmp-x"])
async def test_bad_keys_are_rejected(objects: ObjectStore, key: str) -> None:
    """四个方法都先校验键，不合法的抛 ValueError。"""
    with pytest.raises(ValueError):
        await objects.put(key, b"x")
    with pytest.raises(ValueError):
        await objects.get(key)
    with pytest.raises(ValueError):
        await objects.exists(key)
    with pytest.raises(ValueError):
        await objects.delete(key)


async def test_implementations_satisfy_protocol(objects: ObjectStore) -> None:
    """两个实现都满足 ObjectStore 协议。"""
    assert isinstance(objects, ObjectStore)


async def test_s3_keys_carry_prefix(s3_server: S3Settings) -> None:
    """S3 实现写进桶里的键带上了 prefix。"""
    await S3ObjectStore(s3_server).put("tenants/acme/a.bin", b"x", content_type="text/plain")
    client = _raw_client(s3_server.endpoint)
    listed = client.list_objects_v2(Bucket=s3_server.bucket)
    assert [item["Key"] for item in listed["Contents"]] == ["p0/tenants/acme/a.bin"]
    head = client.head_object(Bucket=s3_server.bucket, Key="p0/tenants/acme/a.bin")
    assert head["ContentType"] == "text/plain"


async def test_s3_missing_bucket_is_not_object_not_found(moto_endpoint: str) -> None:
    """桶不存在时 get 抛 ClientError（NoSuchBucket），不被当成对象不存在。"""
    from botocore.exceptions import ClientError

    store = S3ObjectStore(_settings(moto_endpoint, "no-such-bucket-xyz"))
    with pytest.raises(ClientError) as caught:
        await store.get("a/b")
    assert caught.value.response["Error"]["Code"] == "NoSuchBucket"


async def test_s3_exists_propagates_other_errors() -> None:
    """403 之类的错误不被吞：exists 原样抛 ClientError。"""
    from botocore.exceptions import ClientError

    store = S3ObjectStore(_settings("https://example.invalid", style="virtual"))
    _intercept(store, "HeadObject", 403)
    with pytest.raises(ClientError):
        await store.exists("a/b")


async def test_s3_put_avoids_aws_chunked() -> None:
    """HTTPS 下 put_object 不带 aws-chunked / chunked / 校验和尾部（阿里云 OSS 不支持）。"""
    store = S3ObjectStore(_settings("https://example.invalid", style="virtual"))
    seen = _intercept(store, "PutObject", 200)
    await store.put("a/b", b"data")
    assert len(seen) == 1
    headers = {str(k).lower(): str(v).lower() for k, v in seen[0].headers.items()}
    assert "aws-chunked" not in " ".join(headers.values())
    assert headers.get("transfer-encoding") != "chunked"
    assert "x-amz-trailer" not in headers


def test_s3_without_boto3_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """没装 boto3 时构造 S3 实现抛 ConfigError。"""
    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.setitem(sys.modules, "boto3.session", None)
    with pytest.raises(ConfigError, match="xuanpu-server\\[s3\\]"):
        S3ObjectStore(_settings("http://x"))


def test_s3_invalid_endpoint_is_config_error() -> None:
    """boto3 拒绝的设置（如 endpoint 不合法）包成 ConfigError。"""
    with pytest.raises(ConfigError):
        S3ObjectStore(_settings("not-a-url"))


def test_s3_settings_repr_hides_secrets() -> None:
    """S3Settings 的 repr 不含两个密钥的取值。"""
    text = repr(
        S3Settings(
            endpoint="http://x",
            bucket="b",
            region="r",
            access_key_id="AKID-VALUE",
            secret_access_key="SECRET-VALUE",
        )
    )
    assert "AKID-VALUE" not in text
    assert "SECRET-VALUE" not in text


async def test_local_write_leaves_no_temp_files(tmp_path: Path) -> None:
    """本机实现的写入是原子的：写完目录里只有目标文件。"""
    store = LocalObjectStore(tmp_path / "objects")
    await store.put("tenants/acme/a.bin", b"1")
    await store.put("tenants/acme/a.bin", b"2")
    files = sorted(p.name for p in (tmp_path / "objects").rglob("*") if p.is_file())
    assert files == ["a.bin"]


async def test_local_failed_write_cleans_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本机写入在替换这一步失败时，临时文件被清掉，错误原样上抛。"""

    def boom(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("xuanpu_server.objectstore.local.os.replace", boom)
    store = LocalObjectStore(tmp_path / "objects")
    with pytest.raises(OSError, match="disk full"):
        await store.put("t/a", b"x")
    assert [p for p in (tmp_path / "objects").rglob("*") if p.is_file()] == []


async def test_local_rejects_symlink_escape(tmp_path: Path) -> None:
    """root 下指向外部的符号链接不能被用来越界读写。"""
    root = tmp_path / "objects"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"s")
    (root / "link").symlink_to(outside)
    store = LocalObjectStore(root)
    with pytest.raises(ValueError):
        await store.get("link/secret")
    with pytest.raises(ValueError):
        await store.put("link/new", b"x")
    assert not (outside / "new").exists()


async def test_local_delete_through_a_file_is_not_an_error(tmp_path: Path) -> None:
    """删 ``c/d`` 而 ``c`` 是文件时，视为不存在。"""
    store = LocalObjectStore(tmp_path / "objects")
    await store.put("c", b"x")
    await store.delete("c/d")
    assert not await store.exists("c/d")


async def test_bounded_get_rejects_oversized_body(objects: ObjectStore) -> None:
    """流式读取必须在上限处拒绝，不相信数据库记录的文件大小。"""
    from xuanpu_server.objectstore.base import ObjectTooLargeError

    await objects.put("tenants/acme/bounded.bin", b"12345")
    assert await objects.get_limited("tenants/acme/bounded.bin", max_bytes=5) == b"12345"
    with pytest.raises(ObjectTooLargeError):
        await objects.get_limited("tenants/acme/bounded.bin", max_bytes=4)

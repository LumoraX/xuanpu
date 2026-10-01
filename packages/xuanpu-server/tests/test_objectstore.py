"""对象存储：本机目录与 S3 兼容两种实现共用一组用例。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from xuanpu_server.objectstore import (
    LocalObjectStore,
    ObjectNotFoundError,
    ObjectStore,
    S3ObjectStore,
    S3Settings,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

BUCKET = "xuanpu-test"


def _settings(host: str, port: int) -> S3Settings:
    """指向本机 moto 服务的 S3 设置。"""
    return S3Settings(
        endpoint=f"http://{host}:{port}",
        bucket=BUCKET,
        region="us-east-1",
        access_key_id="test",
        secret_access_key="test",
        prefix="p0/",
        addressing_style="path",
    )


@contextmanager
def moto_s3() -> Iterator[S3Settings]:
    """起一个 moto 的 S3 服务并建好桶，给出连它的设置。"""
    import boto3
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    try:
        settings = _settings(host, port)
        boto3.client(
            "s3",
            endpoint_url=settings.endpoint,
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        ).create_bucket(Bucket=BUCKET)
        yield settings
    finally:
        server.stop()


@pytest.fixture
def s3_server() -> Iterator[S3Settings]:
    """moto 的 S3 服务与连它的设置。"""
    with moto_s3() as settings:
        yield settings


@pytest.fixture(params=["local", "s3"])
async def objects(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[ObjectStore]:
    """本机目录实现，与经 moto 起的 S3 服务上的 S3 实现。"""
    if request.param == "local":
        yield LocalObjectStore(tmp_path / "objects")
        return
    with moto_s3() as settings:
        yield S3ObjectStore(settings)


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
    assert (caught.value.code, caught.value.status) == ("object_not_found", 404)
    assert not await objects.exists("tenants/acme/nope")


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "a//b", "a\\b", "a/./b"])
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
    import boto3

    await S3ObjectStore(s3_server).put("tenants/acme/a.bin", b"x", content_type="text/plain")
    client = boto3.client(
        "s3",
        endpoint_url=s3_server.endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    listed = client.list_objects_v2(Bucket=BUCKET)
    assert [item["Key"] for item in listed["Contents"]] == ["p0/tenants/acme/a.bin"]
    head = client.head_object(Bucket=BUCKET, Key="p0/tenants/acme/a.bin")
    assert head["ContentType"] == "text/plain"


async def test_s3_other_errors_propagate() -> None:
    """桶不存在以外的错误不被吞：连不上的服务让 exists 抛出原始异常。"""
    from botocore.exceptions import BotoCoreError, ClientError

    store = S3ObjectStore(_settings("127.0.0.1", 1))
    with pytest.raises((BotoCoreError, ClientError)):
        await store.exists("a/b")


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

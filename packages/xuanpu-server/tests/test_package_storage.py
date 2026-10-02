"""业务包的存放：归档进对象存储，各实例用到时解到自己的本地缓存并核对指纹。"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from xuanpu_server.api import create_app
from xuanpu_server.errors import (
    ConflictError,
    NotFoundError,
    PackageInvalidError,
    RuntimeUnavailableError,
)
from xuanpu_server.package_cache import archive_key
from xuanpu_server.platform import Platform
from xuanpu_server.runtime import LocalSandboxProvider
from xuanpu_server.tenancy import generate_root_key

if TYPE_CHECKING:
    from pathlib import Path


def _other_instance(world: Any, data_dir: Path) -> Platform:
    """另一个实例：数据目录不同，控制面、密钥保管库与对象存储和 ``world`` 共用。"""
    return Platform(
        store=world.platform.store,
        identity_store=world.platform.identity.store,
        identity_settings=world.platform.identity.settings,
        vault=world.platform.vault,
        models=world.models,
        sandboxes=LocalSandboxProvider(),
        data_dir=data_dir,
        root_key_hash=generate_root_key()[1],
        objects=world.objects,
    )


def _files_under(path: Path) -> list[Path]:
    """某个目录下的全部文件；目录不存在时为空。"""
    return [item for item in path.rglob("*") if item.is_file()] if path.exists() else []


def _failing_registration(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """让登记进控制面这一步失败。"""

    async def failing(record: Any) -> None:
        raise RuntimeError("控制面写入失败")

    monkeypatch.setattr(world.platform.store, "add_package_version", failing)


# --- 发布与解析 ---


async def test_published_archive_lives_in_object_storage(world: Any) -> None:
    """发布后归档在对象存储里，记录的 location 是对象键；键里带归档指纹（十六进制部分）。"""
    await world.publish()
    record = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
    hexdigest = record.digest.removeprefix("sha256:")
    assert record.location == f"tenants/acme/packages/support-desk/1.0.0/{hexdigest}.zip"
    assert record.location == world.archive_key()
    assert await world.objects.exists(record.location)
    assert world.object_keys() == [record.location]


async def test_publishing_instance_reuses_staging_without_download(
    world: Any, object_gets: list[str]
) -> None:
    """发布的实例把校验过的暂存目录直接转正进缓存：之后解析不用再从对象存储下载。"""
    await world.publish()
    resolved = await world.platform.packages.resolve("acme", "support-desk")
    assert object_gets == []
    assert resolved.root.is_relative_to(world.data_dir / "cache" / "packages")
    assert (resolved.root / "xuanpu.yaml").is_file()
    assert resolved.skills_dir == resolved.root / "skills"


async def test_another_instance_unpacks_from_object_storage(world: Any, tmp_path: Path) -> None:
    """另一个实例（不同的数据目录，同一个控制面与对象存储）拿到同一个包，从对象存储解到自己的缓存。"""
    await world.publish()
    other_dir = (tmp_path / "other-data").resolve()
    other = _other_instance(world, other_dir)
    resolved = await other.packages.resolve("acme", "support-desk")
    assert resolved.root.is_relative_to(other_dir / "cache" / "packages")
    assert (resolved.root / "xuanpu.yaml").is_file()
    assert (resolved.root / "skills" / "triage" / "SKILL.md").is_file()


async def test_tampered_archive_is_refused(world: Any, tmp_path: Path) -> None:
    """对象存储里的归档与登记的指纹不符：拒绝解包，报 package_invalid。"""
    await world.publish()
    record = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
    await world.objects.put(record.location, b"not the archive")
    other_dir = (tmp_path / "o2").resolve()
    other = _other_instance(world, other_dir)
    with pytest.raises(PackageInvalidError):
        await other.packages.resolve("acme", "support-desk")
    assert _files_under(other_dir / "cache") == []


async def test_missing_archive_is_a_server_error(world: Any) -> None:
    """登记了、对象存储里却没有归档，本地缓存也没了：报运行时不可用（5xx），不是 404。"""
    await world.publish()
    await world.objects.delete(world.archive_key())
    shutil.rmtree(world.data_dir / "cache")
    with pytest.raises(RuntimeUnavailableError) as caught:
        await world.platform.packages.resolve("acme", "support-desk")
    assert not isinstance(caught.value, NotFoundError)
    assert caught.value.status >= 500


async def test_missing_archive_over_http_is_503(world: Any) -> None:
    """经 HTTP 建会话时归档缺失：返回 503 runtime_unavailable，而不是 404。"""
    await world.publish()
    await world.objects.delete(world.archive_key())
    shutil.rmtree(world.data_dir / "cache")
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with httpx.AsyncClient(transport=transport, base_url="http://xuanpu") as http:
        response = await http.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {world.member_key}"},
            json={"package": "support-desk"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_unavailable"


async def test_concurrent_resolves_unpack_once(world: Any, object_gets: list[str]) -> None:
    """缓存被清空后并发解析同一个包只解一次，结果一致。"""
    await world.publish()
    shutil.rmtree(world.data_dir / "cache")
    roots = await asyncio.gather(
        *[world.platform.packages.resolve("acme", "support-desk") for _ in range(5)]
    )
    assert len({item.root for item in roots}) == 1
    assert object_gets == [world.archive_key()]
    assert (roots[0].root / "xuanpu.yaml").is_file()


# --- 对象存储出错 ---


async def test_object_store_outage_on_read_is_retryable(
    world: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """取归档时对象存储出错（连接失败之类）：报 503（可重试），原异常挂在 __cause__ 上。"""
    await world.publish()

    async def broken_get(key: str) -> bytes:
        raise ConnectionError("https://oss.example.com 连不上")

    monkeypatch.setattr(world.objects, "get", broken_get)
    other = _other_instance(world, (tmp_path / "o3").resolve())
    with pytest.raises(RuntimeUnavailableError) as caught:
        await other.packages.resolve("acme", "support-desk")
    assert isinstance(caught.value.__cause__, ConnectionError)
    assert "oss.example.com" not in caught.value.message


async def test_object_store_outage_on_publish_is_retryable(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布时写归档失败：报 503（可重试），控制面里没有登记，缓存里也没有东西。"""

    async def broken_put(key: str, data: bytes, *, content_type: str = "") -> None:
        raise ConnectionError("https://oss.example.com 连不上")

    monkeypatch.setattr(world.objects, "put", broken_put)
    with pytest.raises(RuntimeUnavailableError) as caught:
        await world.publish()
    assert isinstance(caught.value.__cause__, ConnectionError)
    assert await world.platform.packages.list_versions("acme") == []
    assert world.object_keys() == []
    assert _files_under(world.data_dir / "cache") == []


# --- 登记失败与并发发布 ---


async def test_failed_registration_leaves_nothing(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登记进控制面失败：对象存储里没有残留的归档，缓存目录里也没有残留，原始异常照常抛出。"""
    _failing_registration(world, monkeypatch)
    with pytest.raises(RuntimeError, match="控制面写入失败"):
        await world.publish()
    assert world.object_keys() == []
    assert _files_under(world.data_dir / "cache") == []
    assert await world.platform.packages.list_versions("acme") == []


async def test_cleanup_failure_is_logged_without_hiding_the_cause(
    world: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """登记失败后清理归档也失败：记错误日志，抛出的仍是登记失败的原始异常，缓存里没有东西。"""
    _failing_registration(world, monkeypatch)

    async def broken_delete(key: str) -> None:
        raise OSError("对象存储不可达")

    monkeypatch.setattr(world.objects, "delete", broken_delete)
    with (
        caplog.at_level(logging.ERROR, logger="xuanpu_server.packages"),
        pytest.raises(RuntimeError, match="控制面写入失败"),
    ):
        await world.publish()
    assert any("对象存储不可达" in (record.exc_text or "") for record in caplog.records)
    assert _files_under(world.data_dir / "cache") == []


async def test_registration_that_actually_succeeded_keeps_the_archive(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登记报错、但控制面里其实已经有这条记录（提交后连接断开）：归档留着，不删正在用的东西。"""
    original = world.platform.store.add_package_version

    async def committed_then_failed(record: Any) -> None:
        await original(record)
        raise ConnectionError("提交后连接断开")

    monkeypatch.setattr(world.platform.store, "add_package_version", committed_then_failed)
    with pytest.raises(ConnectionError):
        await world.publish()
    assert world.object_keys() == [world.archive_key()]
    resolved = await world.platform.packages.resolve("acme", "support-desk", "1.0.0")
    assert (resolved.root / "xuanpu.yaml").is_file()


async def test_concurrent_publishes_of_one_version_on_one_instance(world: Any) -> None:
    """同一实例上并发发布同一个版本：只有一个成功，另一个报冲突，且不动成功者的归档。"""
    archive = world.archive()
    results = await asyncio.gather(
        world.platform.packages.publish(world.admin, archive),
        world.platform.packages.publish(world.admin, archive),
        return_exceptions=True,
    )
    errors = [item for item in results if isinstance(item, BaseException)]
    assert len(errors) == 1
    assert getattr(errors[0], "code", None) == "conflict"
    assert world.object_keys() == [world.archive_key()]
    resolved = await world.platform.packages.resolve("acme", "support-desk", "1.0.0")
    assert (resolved.root / "xuanpu.yaml").is_file()


async def test_other_instance_registers_different_content_first(
    world: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """查重之后、登记之前，另一个实例以不同内容抢先发布了同一版本：本次报冲突，
    撤回自己写的归档；对象存储里只剩登记记录指向的那个对象，本实例解析得到对方的内容。"""
    other = _other_instance(world, (tmp_path / "other-data").resolve())
    original = world.platform.store.add_package_version
    raced = False

    async def racing(record: Any) -> None:
        nonlocal raced
        if not raced:
            # 只在本次发布登记时插一次队；对方自己的登记直接放行
            raced = True
            await other.packages.publish(world.admin, world.archive(approval="strict"))
        await original(record)

    monkeypatch.setattr(world.platform.store, "add_package_version", racing)
    with pytest.raises(ConflictError):
        await world.publish()

    winner = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
    assert winner.location == world.archive_key(approval="strict")
    assert winner.location != world.archive_key()
    assert world.object_keys() == [winner.location]
    assert _files_under(world.data_dir / "cache") == []
    resolved = await world.platform.packages.resolve("acme", "support-desk", "1.0.0")
    assert resolved.record.digest == winner.digest
    assert resolved.manifest.spec.policy.approval == "strict"


# --- 登记之后转正进缓存 ---


async def test_cache_failure_after_registration_does_not_fail_publish(
    world: Any,
    monkeypatch: pytest.MonkeyPatch,
    object_gets: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """登记之后转正进本机缓存失败：发布照常成功（记 warning），下次用到时从对象存储重建。"""
    cache = world.platform.packages._cache  # noqa: SLF001 —— 要弄坏的是缓存的安装步骤
    original = cache._install  # noqa: SLF001

    def broken_install(record: Any, unpacked: Path) -> None:
        raise OSError("磁盘满")

    monkeypatch.setattr(cache, "_install", broken_install)
    with caplog.at_level(logging.WARNING, logger="xuanpu_server.packages"):
        await world.publish()
    assert any(item.levelno == logging.WARNING for item in caplog.records)
    assert await world.platform.packages.active_version("acme", "support-desk") == "1.0.0"

    monkeypatch.setattr(cache, "_install", original)
    resolved = await world.platform.packages.resolve("acme", "support-desk")
    assert object_gets == [world.archive_key()]
    assert (resolved.root / "xuanpu.yaml").is_file()


async def test_adopt_keeps_a_cache_that_is_already_current(
    world: Any, monkeypatch: pytest.MonkeyPatch, object_gets: list[str]
) -> None:
    """登记之后、转正之前已有解析把这个版本装进了缓存（可能正在用）：转正时不去覆盖它。"""
    original = world.platform.store.add_package_version

    async def add_then_resolve(record: Any) -> None:
        await original(record)
        resolved = await world.platform.packages.resolve(
            record.tenant_id, record.name, record.version
        )
        (resolved.root / "in-use.txt").write_text("运行时正在用")

    monkeypatch.setattr(world.platform.store, "add_package_version", add_then_resolve)
    await world.publish()
    root = (await world.platform.packages.resolve("acme", "support-desk")).root
    assert (root / "in-use.txt").is_file()
    assert object_gets == [world.archive_key()]


async def test_start_clears_leftover_staging(world: Any, tmp_path: Path) -> None:
    """平台启动时清掉上次进程留下的暂存目录残留（前提：数据目录归本进程独占）。"""
    data_dir = (tmp_path / "restarted").resolve()
    leftover = data_dir / "cache" / ".staging" / "stg_leftover"
    leftover.mkdir(parents=True)
    (leftover / "half.txt").write_text("进程被杀时没收完")
    other = _other_instance(world, data_dir)
    await other.start()
    try:
        assert not leftover.exists()
    finally:
        await other.close()


@pytest.mark.parametrize("digest", ["md5:" + "0" * 32, "sha256:" + "0" * 63, "sha256:" + "G" * 64])
def test_archive_key_requires_a_sha256_digest(digest: str) -> None:
    """归档键里的指纹只认 sha256 的十六进制形状，别的形状直接报错，不拼出奇怪的键。"""
    with pytest.raises(ValueError, match="不认识的归档指纹"):
        archive_key("acme", "support-desk", "1.0.0", digest)

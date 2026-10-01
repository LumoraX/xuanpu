"""业务包的存放：归档进对象存储，各实例用到时解到自己的本地缓存并核对指纹。"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.errors import (
    ConflictError,
    NotFoundError,
    PackageInvalidError,
    RuntimeUnavailableError,
)
from xuanpu_server.packages import archive_key
from xuanpu_server.platform import Platform
from xuanpu_server.runtime import LocalSandboxProvider
from xuanpu_server.tenancy import generate_root_key
from xuanpu_spec import archive_digest

if TYPE_CHECKING:
    from pathlib import Path


def _expected_key(world: Any, **kwargs: Any) -> str:
    """样例业务包（按 ``world.archive`` 的参数打包）在对象存储里应有的键：版本目录下按指纹命名。"""
    hexdigest = archive_digest(world.archive(**kwargs)).removeprefix("sha256:")
    return f"tenants/acme/packages/support-desk/1.0.0/{hexdigest}.zip"


def _other_instance(world: Any, data_dir: Path) -> Platform:
    """另一个实例：数据目录不同，控制面、密钥保管库与对象存储和 ``world`` 共用。"""
    return Platform(
        store=world.platform.store,
        vault=world.platform.vault,
        models=world.models,
        sandboxes=LocalSandboxProvider(),
        data_dir=data_dir,
        root_key_hash=generate_root_key()[1],
        objects=world.objects,
    )


def _count_gets(world: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把对象存储的 ``get`` 换成计数的版本，返回记录每次所取键的列表。"""
    seen: list[str] = []
    original = world.objects.get

    async def counting(key: str) -> bytes:
        seen.append(key)
        # 让出一次事件循环，给并发的解析留出交错的机会
        await asyncio.sleep(0)
        return await original(key)

    monkeypatch.setattr(world.objects, "get", counting)
    return seen


def _files_under(path: Path) -> list[Path]:
    """某个目录下的全部文件；目录不存在时为空。"""
    return [item for item in path.rglob("*") if item.is_file()] if path.exists() else []


def _object_keys(world: Any) -> list[str]:
    """对象存储里现有的全部键。world 夹具的本机对象存储放在数据目录旁边的 objects/。"""
    root = world.data_dir.parent / "objects"
    return sorted(path.relative_to(root).as_posix() for path in _files_under(root))


async def test_published_archive_lives_in_object_storage(world: Any) -> None:
    """发布后归档在对象存储里，记录的 location 是对象键；键里带归档指纹（十六进制部分）。"""
    await world.publish()
    record = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
    hexdigest = record.digest.removeprefix("sha256:")
    assert record.location == f"tenants/acme/packages/support-desk/1.0.0/{hexdigest}.zip"
    assert record.location == _expected_key(world)
    assert await world.objects.exists(record.location)
    assert _object_keys(world) == [record.location]


async def test_publishing_instance_reuses_staging_without_download(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布的实例把校验过的暂存目录直接转正进缓存：之后解析不用再从对象存储下载。"""
    await world.publish()
    seen = _count_gets(world, monkeypatch)
    resolved = await world.platform.packages.resolve("acme", "support-desk")
    assert seen == []
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
    await world.objects.delete(_expected_key(world))
    shutil.rmtree(world.data_dir / "cache")
    with pytest.raises(RuntimeUnavailableError) as caught:
        await world.platform.packages.resolve("acme", "support-desk")
    assert not isinstance(caught.value, NotFoundError)
    assert caught.value.status >= 500


async def test_concurrent_resolves_unpack_once(world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存被清空后并发解析同一个包只解一次，结果一致。"""
    await world.publish()
    shutil.rmtree(world.data_dir / "cache")
    seen = _count_gets(world, monkeypatch)
    roots = await asyncio.gather(
        *[world.platform.packages.resolve("acme", "support-desk") for _ in range(5)]
    )
    assert len({item.root for item in roots}) == 1
    assert seen == [_expected_key(world)]
    assert (roots[0].root / "xuanpu.yaml").is_file()


@pytest.mark.parametrize("damage", ["missing", "mismatch"])
async def test_damaged_cache_is_rebuilt(
    world: Any, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """缓存目录在、但指纹文件缺失或内容不符：从对象存储重建，目录里的杂物被清掉。"""
    await world.publish()
    root = (await world.platform.packages.resolve("acme", "support-desk")).root
    marker = root.parent / ".1.0.0.digest"
    assert marker.is_file()
    if damage == "missing":
        marker.unlink()
    else:
        marker.write_text("sha256:" + "0" * 64)
    (root / "stray.txt").write_text("半截的旧缓存")
    seen = _count_gets(world, monkeypatch)

    rebuilt = await world.platform.packages.resolve("acme", "support-desk")
    assert seen == [_expected_key(world)]
    assert rebuilt.root == root
    assert not (root / "stray.txt").exists()
    assert (root / "xuanpu.yaml").is_file()
    record = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
    assert marker.read_text() == record.digest


async def test_failed_registration_leaves_nothing(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登记进控制面失败：对象存储里没有残留的归档，缓存目录里也没有残留，原始异常照常抛出。"""

    async def failing(record: Any) -> None:
        raise RuntimeError("控制面写入失败")

    monkeypatch.setattr(world.platform.store, "add_package_version", failing)
    with pytest.raises(RuntimeError, match="控制面写入失败"):
        await world.publish()
    assert _object_keys(world) == []
    assert _files_under(world.data_dir / "cache") == []
    assert await world.platform.packages.list_versions("acme") == []


async def test_cleanup_failure_is_logged_without_hiding_the_cause(
    world: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """登记失败后清理归档也失败：记错误日志，抛出的仍是登记失败的原始异常，缓存照样清掉。"""

    async def failing(record: Any) -> None:
        raise RuntimeError("控制面写入失败")

    async def broken_delete(key: str) -> None:
        raise OSError("对象存储不可达")

    monkeypatch.setattr(world.platform.store, "add_package_version", failing)
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
    """登记报错、但控制面里其实已经有这条记录（提交后连接断开）：归档与缓存都留着，不删正在用的东西。"""
    original = world.platform.store.add_package_version

    async def committed_then_failed(record: Any) -> None:
        await original(record)
        raise ConnectionError("提交后连接断开")

    monkeypatch.setattr(world.platform.store, "add_package_version", committed_then_failed)
    with pytest.raises(ConnectionError):
        await world.publish()
    assert _object_keys(world) == [_expected_key(world)]
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
    assert _object_keys(world) == [_expected_key(world)]
    resolved = await world.platform.packages.resolve("acme", "support-desk", "1.0.0")
    assert (resolved.root / "xuanpu.yaml").is_file()


async def test_other_instance_registers_different_content_first(
    world: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """查重之后、登记之前，另一个实例以不同内容抢先发布了同一版本：本次报冲突，
    撤回自己写的归档与缓存；对象存储里只剩登记记录指向的那个对象，本实例解析得到对方的内容。"""
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
    assert winner.location == _expected_key(world, approval="strict")
    assert winner.location != _expected_key(world)
    assert _object_keys(world) == [winner.location]
    assert _files_under(world.data_dir / "cache") == []
    resolved = await world.platform.packages.resolve("acme", "support-desk", "1.0.0")
    assert resolved.record.digest == winner.digest
    assert resolved.manifest.spec.policy.approval == "strict"


@pytest.mark.parametrize("digest", ["md5:" + "0" * 32, "sha256:" + "0" * 63, "sha256:" + "G" * 64])
def test_archive_key_requires_a_sha256_digest(digest: str) -> None:
    """归档键里的指纹只认 sha256 的十六进制形状，别的形状直接报错，不拼出奇怪的键。"""
    with pytest.raises(ValueError, match="不认识的归档指纹"):
        archive_key("acme", "support-desk", "1.0.0", digest)

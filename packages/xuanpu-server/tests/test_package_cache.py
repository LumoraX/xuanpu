"""业务包本地缓存的健壮性：坏了能重建、安装不可中断、路径与键的纵深防御。"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import shutil
import threading
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.errors import RuntimeUnavailableError
from xuanpu_server.package_cache import run_to_completion
from xuanpu_spec import unpack_package

if TYPE_CHECKING:
    from pathlib import Path


def _files(root: Path) -> list[str]:
    """目录下全部文件的相对路径，已排序；目录不存在时为空。"""
    if not root.exists():
        return []
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


# --- 线程里的工作不可中断 ---


async def test_run_to_completion_returns_and_raises_normally() -> None:
    """没有取消时，结果与异常照常传回调用方。"""
    assert await run_to_completion(lambda left, right: left + right, 1, 2) == 3

    def boom() -> None:
        raise OSError("磁盘满")

    with pytest.raises(OSError, match="磁盘满"):
        await run_to_completion(boom)


async def test_run_to_completion_defers_cancellation_until_the_thread_finishes() -> None:
    """取消要等线程跑完才生效，且照常抛出；等待期间再来一次取消也一样。"""
    started, release = threading.Event(), threading.Event()
    finished: list[bool] = []

    def work() -> None:
        started.set()
        assert release.wait(timeout=10)
        finished.append(True)

    task = asyncio.create_task(run_to_completion(work))
    assert await asyncio.to_thread(started.wait, 10)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == [True]


async def test_cancelled_install_holds_the_lock_until_the_thread_finishes(
    world: Any, monkeypatch: pytest.MonkeyPatch, object_gets: list[str], tmp_path: Path
) -> None:
    """安装卡在线程里时取消外层任务：取消要等线程跑完才生效，期间锁一直持有，另一个解析
    拿不到锁、也不会重复下载；最后指纹在、目录完整，暂存目录收干净，取消照常抛出。"""
    await world.publish()
    shutil.rmtree(world.data_dir / "cache")
    cache = world.platform.packages._cache  # noqa: SLF001 —— 要卡住的是缓存的安装步骤
    original = cache._install  # noqa: SLF001
    entered, release = threading.Event(), threading.Event()

    def blocking_install(record: Any, unpacked: Path) -> None:
        entered.set()
        assert release.wait(timeout=10)
        original(record, unpacked)

    monkeypatch.setattr(cache, "_install", blocking_install)
    first = asyncio.create_task(world.platform.packages.resolve("acme", "support-desk"))
    assert await asyncio.to_thread(entered.wait, 10)
    first.cancel()
    second = asyncio.create_task(world.platform.packages.resolve("acme", "support-desk"))
    await asyncio.sleep(0.1)
    record = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
    assert not first.done()
    assert not second.done()
    assert cache._lock(record).locked()  # noqa: SLF001

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    resolved = await second
    assert object_gets == [world.archive_key()]
    expected = tmp_path / "expected"
    unpack_package(world.archive(), expected)
    assert _files(resolved.root) == _files(expected)
    assert (resolved.root.parent / ".1.0.0.digest").read_text() == record.digest
    assert _files(world.data_dir / "cache" / ".staging") == []


# --- 缓存坏了能重建 ---


@pytest.mark.parametrize(
    "damage",
    ["marker_missing", "marker_mismatch", "directory_missing", "file_in_place", "symlink_in_place"],
)
async def test_damaged_cache_is_rebuilt(
    world: Any, object_gets: list[str], tmp_path: Path, damage: str
) -> None:
    """缓存坏了（指纹缺失或不符；指纹在、目录不在；目录位置被普通文件或符号链接占了）：
    从对象存储重建，目录里的杂物被清掉；符号链接只删链接本身，不碰它指向的目录。"""
    await world.publish()
    root = (await world.platform.packages.resolve("acme", "support-desk")).root
    marker = root.parent / ".1.0.0.digest"
    assert marker.is_file()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("别动我")
    if damage == "marker_missing":
        marker.unlink()
    elif damage == "marker_mismatch":
        marker.write_text("sha256:" + "0" * 64)
    else:
        shutil.rmtree(root)
        if damage == "file_in_place":
            root.write_text("不是目录")
        elif damage == "symlink_in_place":
            root.symlink_to(outside, target_is_directory=True)
    if root.is_dir() and not root.is_symlink():
        (root / "stray.txt").write_text("半截的旧缓存")

    rebuilt = await world.platform.packages.resolve("acme", "support-desk")
    assert object_gets == [world.archive_key()]
    assert rebuilt.root == root
    assert root.is_dir()
    assert not root.is_symlink()
    assert not (root / "stray.txt").exists()
    assert (root / "xuanpu.yaml").is_file()
    assert _files(outside) == ["keep.txt"]
    record = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
    assert marker.read_text() == record.digest


# --- 纵深防御 ---


async def test_location_must_match_the_record(
    world: Any, object_gets: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """控制面记录被改坏、location 指向别的租户的归档：拒绝读取（503）并记错误日志，不碰那个对象。"""
    await world.publish()
    record = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
    await world.platform.store.add_package_version(dataclasses.replace(record, tenant_id="globex"))
    with (
        caplog.at_level(logging.ERROR, logger="xuanpu_server.package_cache"),
        pytest.raises(RuntimeUnavailableError),
    ):
        await world.platform.packages.resolve("globex", "support-desk", "1.0.0")
    assert object_gets == []
    assert any(item.levelno == logging.ERROR for item in caplog.records)
    assert list((world.data_dir / "cache" / "packages").glob("globex-*")) == []


@pytest.mark.parametrize(
    ("field", "value"), [("name", "../../escape"), ("name", "a/b"), ("version", "..")]
)
async def test_record_fields_must_be_plain_directory_names(
    world: Any, object_gets: list[str], field: str, value: str
) -> None:
    """记录里的包名或版本号不能当单级目录名（控制面数据被改坏）：报 503，不读对象存储、不落盘。"""
    await world.publish()
    record = (await world.platform.packages.list_versions("acme", "support-desk"))[0]
    forged = dataclasses.replace(record, **{field: value})
    await world.platform.store.add_package_version(forged)
    before = _files(world.data_dir)
    with pytest.raises(RuntimeUnavailableError):
        await world.platform.packages.resolve("acme", forged.name, forged.version)
    assert object_gets == []
    assert _files(world.data_dir) == before

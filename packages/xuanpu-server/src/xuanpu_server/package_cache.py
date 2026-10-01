"""业务包的本地解包缓存。

归档的正本在对象存储里（``PackageVersionRecord.location`` 是它的键，见 ``archive_key``）；
各实例用到某个版本时从对象存储取归档、核对登记的指纹、解到本机缓存。缓存可以在实例停机时
整个清空，下次用到时重建。

目录布局（``root`` 由平台给成 ``<数据目录>/cache/packages``）::

    <root>/<租户目录>/<包名>/<版本>/           解包后的文件
    <root>/<租户目录>/<包名>/.<版本>.digest    这个版本目录对应的归档指纹

指纹文件放在版本目录外面：归档里可以有任意文件名（解包规则不排除 ``.digest``），放在里面
会和包内文件冲突。它的名字以点开头，而版本号必须以数字开头，所以也不会和某个版本目录撞名。

一致性靠两条保证：

- 顺序：装入时先删旧指纹、再换目录、最后写指纹；指纹在且内容相符，版本目录就一定是完整的。
  中途中断（进程被杀、磁盘满）留下的都是「没有指纹或指纹不符」，下次用到时自动重建。
- 不可中断：解包、装入、收暂存目录整段在一个线程里跑完（``run_to_completion``）。调用方被
  取消时要等线程跑完才放开锁，不会出现线程还在改名、外面已经开始删目录的情况。

缓存的锁只在进程内，数据目录必须归一个进程独占。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from typing import TYPE_CHECKING, Any

from xuanpu_spec import ArchiveError, archive_digest, unpack_package

from xuanpu_server.errors import PackageInvalidError, RuntimeUnavailableError
from xuanpu_server.ids import new_id
from xuanpu_server.objectstore import ObjectNotFoundError, validate_key
from xuanpu_server.tenancy import tenant_directory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from xuanpu_server.controlplane.models import PackageVersionRecord
    from xuanpu_server.objectstore import ObjectStore

logger = logging.getLogger(__name__)

CacheKey = tuple[str, str, str]
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_IN_SEGMENT = re.compile(r"[/\\\x00]")


def archive_key(tenant_id: str, name: str, version: str, digest: str) -> str:
    """某个版本的归档在对象存储里的键：``tenants/<租户>/packages/<名>/<版本>/<十六进制>.zip``。

    以租户开头（对象存储的键规则）；带上指纹，内容不同的归档永远是不同的对象。

    Args:
        digest: ``archive_digest`` 给出的指纹，形如 ``sha256:<十六进制>``；键里只放十六进制。

    Raises:
        ValueError: 指纹不是 sha256 的形状，或拼出的键不合法。
    """
    algorithm, _, hexdigest = digest.partition(":")
    if algorithm != "sha256" or not _SHA256_HEX.match(hexdigest):
        raise ValueError(f"不认识的归档指纹：{digest!r}")
    return validate_key(f"tenants/{tenant_id}/packages/{name}/{version}/{hexdigest}.zip")


async def run_to_completion[T](func: Callable[..., T], *args: Any) -> T:
    """在线程里跑一段阻塞工作，并保证它跑完：线程里的工作不可中断，取消要等它跑完再生效。

    ``asyncio.to_thread`` 的线程停不下来——调用方被取消时 ``await`` 立刻返回，线程却还在跑。
    这时调用方若释放锁、清理目录，就会和仍在跑的线程打架，留下半截状态（例如指纹已写、目录
    残缺）。这里先把取消挡在外面，等线程跑完（调用方持有的锁仍然持有），再把取消抛出去。
    """
    future = asyncio.ensure_future(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        await _wait_out(future)
        raise


async def _wait_out(future: asyncio.Future[Any]) -> None:
    """等一个已经挡住取消的线程任务结束；期间再来的取消也一并挡住（调用方最后仍会抛出取消）。

    线程在这期间抛的异常只记日志：这时要抛给调用方的是取消。
    """
    while not future.done():
        # asyncio.wait 被取消时不会连带取消它等的任务
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({future})
    if not future.cancelled() and future.exception() is not None:
        logger.error("取消生效前，线程里的工作失败", exc_info=future.exception())


def unpack_archive(archive: bytes, destination: Path) -> None:
    """把归档解到一个尚不存在（或为空）的目录。阻塞调用，放在线程里跑。

    Raises:
        PackageInvalidError: 归档不合法。
    """
    try:
        unpack_package(archive, destination)
    except ArchiveError as exc:
        raise PackageInvalidError(f"业务包归档不合法：{exc}") from exc


def _remove_path(path: Path) -> None:
    """删掉某个位置上的东西；不存在不算错。阻塞调用。

    目录整棵删；符号链接只删链接本身（不碰它指向的东西）；普通文件等非目录直接 unlink。
    缓存位置被意外的东西占了时也能清掉，不至于每次都失败。
    """
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except NotADirectoryError:
        path.unlink(missing_ok=True)


def _drop_staging(path: Path) -> None:
    """收掉一个暂存目录（已转正的就不在了）。阻塞调用。

    删除失败只记日志：留下的暂存目录不影响正确性（下次启动时清掉），而这一步常在异常传播
    途中执行，不能让它盖掉原始异常。
    """
    try:
        _remove_path(path)
    except OSError:
        logger.exception("删除暂存目录 %s 失败", path)


def _plain_segment(segment: str) -> bool:
    """能直接当单级目录名用：非空、不是 ``.`` 或 ``..``、不含路径分隔符与 NUL。"""
    return segment not in {"", ".", ".."} and not _UNSAFE_IN_SEGMENT.search(segment)


class PackageCache:
    """业务包的本地解包缓存。实例停机时可以整个清空，用到时从对象存储重建。"""

    def __init__(self, objects: ObjectStore, root: Path, *, staging: Path) -> None:
        """
        Args:
            objects: 对象存储，归档的正本在这里。
            root: 缓存根目录。
            staging: 暂存目录的父目录。必须与 ``root`` 在同一个文件系统上：转正靠改名，
                同一个文件系统上改名才是原子的。
        """
        self._objects = objects
        self._root = root
        self._staging = staging
        self._locks: dict[CacheKey, asyncio.Lock] = {}

    def directory(self, record: PackageVersionRecord) -> Path:
        """某个版本解包后的目录：``<root>/<租户目录>/<名>/<版本>``。

        纵深防御：包名与版本号在发布时已按规范的字符集校验过，这里再确认三段都是单级目录名、
        结果在缓存根目录内——控制面数据被改坏也逃不出本租户的缓存目录。

        已知限制：版本号的预发布段可以含大写字母（``1.0.0-RC`` 与 ``1.0.0-rc`` 是两个版本），
        在大小写不敏感的文件系统上（如 macOS 默认）两者的目录会相撞。指纹文件能发现内容不符
        并重建，但两个版本交替使用时会反复互相覆盖。

        Raises:
            RuntimeUnavailableError: 记录里的租户、包名或版本号不能当目录名（控制面数据异常）。
        """
        segments = (tenant_directory(record.tenant_id), record.name, record.version)
        path = self._root.joinpath(*segments)
        if not all(_plain_segment(segment) for segment in segments) or not path.is_relative_to(
            self._root
        ):
            logger.error(
                "业务包记录 %r/%r@%r 不能当缓存目录名，控制面数据可能被改坏",
                record.tenant_id, record.name, record.version,
            )  # fmt: skip
            raise RuntimeUnavailableError("业务包缓存路径异常，请联系平台管理员")
        return path

    def _marker(self, record: PackageVersionRecord) -> Path:
        """某个版本的指纹文件：与版本目录同级的 ``.<版本>.digest``。"""
        return self.directory(record).with_name(f".{record.version}.digest")

    def _lock(self, record: PackageVersionRecord) -> asyncio.Lock:
        """某个版本的锁：同一版本的装入在本进程内串行。"""
        key: CacheKey = (record.tenant_id, record.name, record.version)
        return self._locks.setdefault(key, asyncio.Lock())

    async def clear_staging(self) -> None:
        """清掉暂存目录下的残留（上次进程被杀时没来得及收的）。

        前提：数据目录归本进程独占。别的进程若共用这个数据目录，会删掉它正在用的暂存目录。
        """
        await run_to_completion(_remove_path, self._staging)

    @contextlib.asynccontextmanager
    async def staging(self) -> AsyncIterator[Path]:
        """给出一个新的暂存目录路径（尚未创建），离开时收掉它。

        暂存目录与缓存在同一个文件系统上，``adopt`` 转正时改名是原子的。
        """
        path = self._staging / new_id("stg")
        try:
            yield path
        finally:
            await run_to_completion(_drop_staging, path)

    async def ensure(self, record: PackageVersionRecord) -> Path:
        """返回某个版本的解包目录；缓存里没有或已损坏时从对象存储重建。

        缓存里有（指纹文件的内容等于 ``record.digest`` 且版本目录在）就直接返回；否则在
        按 (租户, 名, 版本) 的锁里：取归档 → 核对键与指纹 → 解到暂存目录 → 改名到位 → 写指纹。
        同一版本的并发调用只有一个真的去取、去解。

        Raises:
            PackageInvalidError: 对象存储里的归档与登记的指纹不符，或归档解不开。
            RuntimeUnavailableError: 归档不在对象存储里、对象存储出错，或记录本身异常。
        """
        final = self.directory(record)
        if await asyncio.to_thread(self._is_current, record):
            return final
        async with self._lock(record):
            # 拿到锁之后再看一次：等锁期间可能已经有人装好了
            if await asyncio.to_thread(self._is_current, record):
                return final
            archive = await self._fetch(record)
            await run_to_completion(self._build, record, archive)
        return final

    async def adopt(self, record: PackageVersionRecord, unpacked: Path) -> Path:
        """发布时已经解过、校验过的暂存目录转正进缓存（省一次下载）。

        缓存里已经是这份内容（例如登记之后、转正之前已有解析把它装好了）就不动，不去覆盖
        可能正在用的目录。

        Args:
            record: 这个目录对应的版本记录；指纹按它写。
            unpacked: 来自 ``staging()`` 的暂存目录。转正后它就不在原处了。
        """
        async with self._lock(record):
            await run_to_completion(self._adopt_blocking, record, unpacked)
        return self.directory(record)

    async def _fetch(self, record: PackageVersionRecord) -> bytes:
        """核对键之后从对象存储取归档，再核对登记的指纹。"""
        self._check_location(record)
        try:
            archive = await self._objects.get(record.location)
        except ObjectNotFoundError as exc:
            logger.error(
                "业务包 %s/%s@%s 已登记，归档 %s 却不在对象存储里",
                record.tenant_id, record.name, record.version, record.location,
            )  # fmt: skip
            raise RuntimeUnavailableError(
                f"业务包 {record.name} 的版本 {record.version} 的归档缺失，请联系平台管理员"
            ) from exc
        except Exception as exc:
            logger.exception("从对象存储取业务包归档 %s 失败", record.location)
            raise RuntimeUnavailableError("对象存储暂时不可用，请稍后重试") from exc
        # 归档最大 64 MiB，算摘要放进线程，不卡事件循环
        if await asyncio.to_thread(archive_digest, archive) != record.digest:
            logger.error(
                "业务包 %s/%s@%s 的归档 %s 与登记的指纹不符",
                record.tenant_id, record.name, record.version, record.location,
            )  # fmt: skip
            raise PackageInvalidError(
                f"业务包 {record.name} 的版本 {record.version} 的归档与登记的指纹不符"
            )
        return archive

    def _check_location(self, record: PackageVersionRecord) -> None:
        """纵深防御：登记的键必须就是按记录本身（租户、名、版本、指纹）算出的那个。

        防止控制面数据被改坏后指向别的租户的归档（红线 5）。

        Raises:
            RuntimeUnavailableError: 键对不上。
        """
        try:
            expected = archive_key(record.tenant_id, record.name, record.version, record.digest)
        except ValueError as exc:
            logger.error(
                "业务包记录 %r/%r@%r 拼不出合法的归档键：%s",
                record.tenant_id, record.name, record.version, exc,
            )  # fmt: skip
            raise RuntimeUnavailableError("业务包记录异常，请联系平台管理员") from exc
        if record.location != expected:
            logger.error(
                "业务包 %s/%s@%s 登记的归档键 %r 与按记录算出的 %r 不符，控制面数据可能被改坏",
                record.tenant_id, record.name, record.version, record.location, expected,
            )  # fmt: skip
            raise RuntimeUnavailableError("业务包记录异常，请联系平台管理员")

    def _is_current(self, record: PackageVersionRecord) -> bool:
        """缓存里的这个版本是否完整可用。阻塞调用，放在线程里跑。

        按字节比较指纹文件：写到一半的指纹文件内容必然不符，会触发重建。版本目录必须是真的
        目录，不能是指向别处的符号链接。
        """
        try:
            recorded = self._marker(record).read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return False
        final = self.directory(record)
        return (
            recorded == record.digest.encode("utf-8") and final.is_dir() and not final.is_symlink()
        )

    def _build(self, record: PackageVersionRecord, archive: bytes) -> None:
        """解包到新的暂存目录 → 装入 → 收掉暂存目录。阻塞调用，整段在同一个线程里完成。"""
        staging = self._staging / new_id("stg")
        try:
            unpack_archive(archive, staging)
            self._install(record, staging)
        finally:
            _drop_staging(staging)

    def _adopt_blocking(self, record: PackageVersionRecord, unpacked: Path) -> None:
        """缓存里已是这份内容就不动，否则装入。阻塞调用，放在线程里跑。"""
        if not self._is_current(record):
            self._install(record, unpacked)

    def _install(self, record: PackageVersionRecord, unpacked: Path) -> None:
        """把解好的目录改名到位并写指纹。阻塞调用，放在线程里跑。

        先删旧指纹、再删旧目录（损坏或过期的旧缓存，或占了位置的文件、链接）、改名、最后
        写指纹：任何一步中断，留下的都是「没有指纹」，下次用到时重建。
        """
        final = self.directory(record)
        marker = self._marker(record)
        final.parent.mkdir(parents=True, exist_ok=True)
        _remove_path(marker)
        _remove_path(final)
        unpacked.rename(final)
        marker.write_bytes(record.digest.encode("utf-8"))


__all__ = ["PackageCache", "archive_key", "run_to_completion", "unpack_archive"]

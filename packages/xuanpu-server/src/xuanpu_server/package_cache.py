"""业务包的本地解包缓存。

归档的正本在对象存储里（``PackageVersionRecord.location`` 是它的键）；各实例用到某个版本时
从对象存储取归档、核对登记的指纹、解到本机缓存。缓存可以随时整个清空，下次用到时重建。

目录布局（``root`` 由平台给成 ``<数据目录>/cache/packages``）::

    <root>/<租户目录>/<包名>/<版本>/           解包后的文件
    <root>/<租户目录>/<包名>/.<版本>.digest    这个版本目录对应的归档指纹

指纹文件放在版本目录外面：归档里可以有任意文件名（解包规则不排除 ``.digest``），放在里面
会和包内文件冲突。它的名字以点开头，而版本号必须以数字开头，所以也不会和某个版本目录撞名。

一致性靠顺序保证：装入时先删旧指纹、再换目录、最后写指纹；指纹在且内容相符，版本目录就
一定是完整的。中途中断（进程被杀、磁盘满）留下的都是「没有指纹或指纹不符」，下次用到时
自动重建。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from typing import TYPE_CHECKING

from xuanpu_spec import ArchiveError, archive_digest, unpack_package

from xuanpu_server.errors import PackageInvalidError, RuntimeUnavailableError
from xuanpu_server.ids import new_id
from xuanpu_server.objectstore import ObjectNotFoundError
from xuanpu_server.tenancy import tenant_directory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from xuanpu_server.controlplane.models import PackageVersionRecord
    from xuanpu_server.objectstore import ObjectStore

logger = logging.getLogger(__name__)

CacheKey = tuple[str, str, str]


def unpack_archive(archive: bytes, destination: Path) -> None:
    """把归档解到一个尚不存在（或为空）的目录。阻塞调用，放在线程里跑。

    Raises:
        PackageInvalidError: 归档不合法。
    """
    try:
        unpack_package(archive, destination)
    except ArchiveError as exc:
        raise PackageInvalidError(f"业务包归档不合法：{exc}") from exc


def _remove_tree(path: Path) -> None:
    """删掉一个目录树；不存在不算错。"""
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(path)


def _drop_staging(path: Path) -> None:
    """收掉一个暂存目录（已转正的就不在了）。

    删除失败只记日志：留下的暂存目录不影响正确性，而这一步常在异常传播途中执行，
    不能让它盖掉原始异常。
    """
    try:
        _remove_tree(path)
    except OSError:
        logger.exception("删除暂存目录 %s 失败", path)


class PackageCache:
    """业务包的本地解包缓存。缓存可以随时清空，用到时从对象存储重建。"""

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

        包名与版本号在发布时已按规范的字符集校验过，不会逃出缓存根目录。
        """
        return self._root / tenant_directory(record.tenant_id) / record.name / record.version

    def _marker(self, record: PackageVersionRecord) -> Path:
        """某个版本的指纹文件：与版本目录同级的 ``.<版本>.digest``。"""
        return self.directory(record).with_name(f".{record.version}.digest")

    def _lock(self, record: PackageVersionRecord) -> asyncio.Lock:
        """某个版本的锁：同一版本的装入与删除在本实例内串行。"""
        key: CacheKey = (record.tenant_id, record.name, record.version)
        return self._locks.setdefault(key, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def staging(self) -> AsyncIterator[Path]:
        """给出一个新的暂存目录路径（尚未创建），离开时收掉它。

        暂存目录与缓存在同一个文件系统上，``adopt`` 转正时改名是原子的。
        """
        path = self._staging / new_id("stg")
        try:
            yield path
        finally:
            await asyncio.to_thread(_drop_staging, path)

    async def ensure(self, record: PackageVersionRecord) -> Path:
        """返回某个版本的解包目录；缓存里没有或已损坏时从对象存储重建。

        缓存里有（指纹文件的内容等于 ``record.digest`` 且版本目录在）就直接返回；否则在
        按 (租户, 名, 版本) 的锁里：取归档 → 核对指纹 → 解到暂存目录 → 改名到位 → 写指纹。
        同一版本的并发调用只有一个真的去取、去解。

        Raises:
            PackageInvalidError: 对象存储里的归档与登记的指纹不符，或归档解不开。
            RuntimeUnavailableError: 版本已登记，归档却不在对象存储里（服务端数据不一致）。
        """
        final = self.directory(record)
        if await asyncio.to_thread(self._is_current, record):
            return final
        async with self._lock(record):
            # 拿到锁之后再看一次：等锁期间可能已经有人装好了
            if await asyncio.to_thread(self._is_current, record):
                return final
            archive = await self._fetch(record)
            async with self.staging() as staging:
                await asyncio.to_thread(unpack_archive, archive, staging)
                await asyncio.to_thread(self._install, record, staging)
        return final

    async def adopt(self, record: PackageVersionRecord, unpacked: Path) -> Path:
        """发布时已经解过、校验过的暂存目录直接转正进缓存（省一次下载）。

        Args:
            record: 这个目录对应的版本记录；指纹按它写。
            unpacked: 来自 ``staging()`` 的暂存目录。转正后它就不在原处了。
        """
        async with self._lock(record):
            await asyncio.to_thread(self._install, record, unpacked)
        return self.directory(record)

    async def discard(self, record: PackageVersionRecord) -> None:
        """删掉某个版本的缓存（先删指纹、再删目录）；不在不算错。"""
        async with self._lock(record):
            await asyncio.to_thread(self._remove, record)

    async def _fetch(self, record: PackageVersionRecord) -> bytes:
        """从对象存储取归档并核对登记的指纹。"""
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

    def _is_current(self, record: PackageVersionRecord) -> bool:
        """缓存里的这个版本是否完整可用。阻塞调用，放在线程里跑。

        按字节比较指纹文件：写到一半的指纹文件内容必然不符，会触发重建。
        """
        try:
            recorded = self._marker(record).read_bytes()
        except FileNotFoundError:
            return False
        return recorded == record.digest.encode("utf-8") and self.directory(record).is_dir()

    def _install(self, record: PackageVersionRecord, unpacked: Path) -> None:
        """把解好的目录改名到位并写指纹。阻塞调用，放在线程里跑。

        先删旧指纹、再删旧目录（损坏或过期的旧缓存）、改名、最后写指纹：任何一步中断，
        留下的都是「没有指纹」，下次用到时重建。
        """
        final = self.directory(record)
        final.parent.mkdir(parents=True, exist_ok=True)
        self._remove(record)
        unpacked.rename(final)
        self._marker(record).write_bytes(record.digest.encode("utf-8"))

    def _remove(self, record: PackageVersionRecord) -> None:
        """删指纹、再删版本目录。阻塞调用，放在线程里跑。"""
        self._marker(record).unlink(missing_ok=True)
        _remove_tree(self.directory(record))


__all__ = ["PackageCache", "unpack_archive"]

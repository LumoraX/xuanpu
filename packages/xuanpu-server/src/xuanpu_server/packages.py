"""业务包登记处：发布、切换生效版本、按租户解析。

发布即不可变：同名同版本不能再次发布，改动必须发新版本。回滚就是把生效版本指回旧版本。

归档的正本放在对象存储里，键是 ``tenants/<租户>/packages/<包名>/<版本>.zip``，登记在
``PackageVersionRecord.location``。各实例不共享数据目录：用到某个版本时由 ``PackageCache``
从对象存储取归档、核对指纹、解到本机缓存 ``<数据目录>/cache/packages/``。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import taifeng
from xuanpu_spec import (
    MANIFEST_FILENAME,
    PackageManifest,
    archive_digest,
    parse_manifest,
    validate_package,
    version_key,
)

from xuanpu_server.controlplane.models import PackageVersionRecord
from xuanpu_server.errors import ConflictError, NotFoundError, PackageInvalidError
from xuanpu_server.objectstore import validate_key
from xuanpu_server.package_cache import PackageCache, unpack_archive

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.objectstore import ObjectStore
    from xuanpu_server.tenancy import Principal

logger = logging.getLogger(__name__)

ARCHIVE_CONTENT_TYPE = "application/zip"


@dataclass(frozen=True)
class ResolvedPackage:
    """一个可以拿去运行的业务包版本。

    Attributes:
        record: 登记的版本记录。
        manifest: 解析好的清单。
        root: 本实例缓存里解包后的目录。
    """

    record: PackageVersionRecord
    manifest: PackageManifest
    root: Path

    @property
    def skills_dir(self) -> Path:
        """skill 目录。"""
        return self.root / self.manifest.spec.skills.dir


def archive_key(tenant_id: str, name: str, version: str) -> str:
    """某个版本的归档在对象存储里的键。以租户开头（对象存储的键规则）。"""
    return validate_key(f"tenants/{tenant_id}/packages/{name}/{version}.zip")


def _unpack_and_validate(archive: bytes, staging: Path) -> tuple[PackageManifest, str]:
    """解包到暂存目录并按规范校验，返回清单与清单原文。阻塞调用，放在线程里跑。"""
    unpack_archive(archive, staging)
    report = validate_package(staging)
    if not report.ok or report.manifest is None:
        raise PackageInvalidError("业务包不符合规范", report.errors)
    return report.manifest, (staging / MANIFEST_FILENAME).read_text(encoding="utf-8")


class PackageRegistry:
    """业务包登记处。"""

    def __init__(
        self,
        store: ControlPlaneStore,
        audit: AuditLog,
        *,
        data_dir: Path,
        objects: ObjectStore,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 控制面存储。
            audit: 审计写入口。
            data_dir: 平台数据目录。业务包缓存在其下的 ``cache/packages/``，发布与重建
                时的暂存目录在 ``cache/.staging/``——两者同在 ``cache/`` 下，保证在同一个
                文件系统上，暂存目录转正时改名是原子的。
            objects: 对象存储，业务包归档的正本放在这里。
            now: 取当前时间的函数，测试时可替换。
        """
        self._store = store
        self._audit = audit
        self._objects = objects
        self._cache = PackageCache(
            objects, data_dir / "cache" / "packages", staging=data_dir / "cache" / ".staging"
        )
        self._now = now
        self._publishing: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def _check_loadable(self, manifest: PackageManifest, staging: Path) -> None:
        """交给内核实际加载一遍 skill：规范校验通过不代表内核认这些 skill。"""
        try:
            registry = await taifeng.FilesystemSkillRegistry.load(
                staging / manifest.spec.skills.dir
            )
        except Exception as exc:  # noqa: BLE001 —— 内核的校验异常不在稳定层，只能按基类接
            raise PackageInvalidError(f"skill 加载失败：{exc}") from exc
        snapshot = registry.snapshot()
        missing = [entry for entry in manifest.spec.skills.entries if snapshot.get(entry) is None]
        if missing:
            raise PackageInvalidError(
                "入口 skill 未能加载", tuple(f"入口 skill {entry!r}" for entry in missing)
            )

    async def publish(
        self, principal: Principal, archive: bytes, *, activate: bool = False
    ) -> PackageVersionRecord:
        """发布一个业务包版本。

        Args:
            principal: 调用者，须是租户管理员。
            archive: 业务包归档。
            activate: 发布后是否立即设为生效版本。

        Raises:
            PackageInvalidError: 归档或包内容不符合规范。
            ConflictError: 同名同版本已经发布过。
        """
        tenant_id = principal.require_tenant_admin()
        async with self._cache.staging() as staging:
            manifest, manifest_yaml = await asyncio.to_thread(
                _unpack_and_validate, archive, staging
            )
            await self._check_loadable(manifest, staging)
            record = PackageVersionRecord(
                tenant_id=tenant_id,
                name=manifest.name,
                version=manifest.version,
                digest=await asyncio.to_thread(archive_digest, archive),
                manifest_yaml=manifest_yaml,
                location=archive_key(tenant_id, manifest.name, manifest.version),
                published_at=self._now(),
                published_by=principal.actor,
            )
            await self._commit(record, archive, staging)
        await self._audit.control(
            principal,
            tenant_id,
            "package_published",
            {"name": record.name, "version": record.version, "digest": record.digest},
        )
        if activate:
            await self.activate(principal, record.name, record.version)
        return record

    async def _commit(self, record: PackageVersionRecord, archive: bytes, staging: Path) -> None:
        """查重 → 归档写进对象存储 → 暂存目录转正进缓存 → 登记。

        本实例内同一版本的发布串行，查重到登记之间不会被自己人插队。任何一步失败都撤回
        已经写下的归档与缓存，不留下没有记录的包。
        """
        key = (record.tenant_id, record.name, record.version)
        async with self._publishing.setdefault(key, asyncio.Lock()):
            if await self._store.get_package_version(*key):
                raise ConflictError(f"业务包 {record.name} 的版本 {record.version} 已发布")
            try:
                await self._objects.put(record.location, archive, content_type=ARCHIVE_CONTENT_TYPE)
                await self._cache.adopt(record, staging)
                await self._store.add_package_version(record)
            except BaseException:
                await self._rollback(record)
                raise

    async def _rollback(self, record: PackageVersionRecord) -> None:
        """登记失败后撤回本次写下的归档与缓存。清理失败只记日志，不盖掉原始异常。

        只有确认控制面里确实没有这个版本时才删：登记可能其实已经成功（提交后连接断开），
        也可能是别的实例在查重之后抢先登记了同一版本，这两种情况下对象存储里的归档都有人
        在用。确认不了时归档留着——没有记录指向它，下次发布同一版本时会被覆盖。
        """
        try:
            registered = await self._store.get_package_version(
                record.tenant_id, record.name, record.version
            )
        except Exception:
            logger.exception(
                "登记失败后无法确认业务包 %s/%s@%s 是否已登记，归档 %s 留在对象存储里",
                record.tenant_id, record.name, record.version, record.location,
            )  # fmt: skip
            return
        if registered is not None:
            if registered.digest != record.digest:
                logger.error(
                    "业务包 %s/%s@%s 被并发发布，登记的指纹与本次不同，归档 %s 可能已被覆盖",
                    record.tenant_id, record.name, record.version, record.location,
                )  # fmt: skip
            return
        try:
            await self._objects.delete(record.location)
        except Exception:
            logger.exception("撤回业务包归档 %s 失败", record.location)
        try:
            await self._cache.discard(record)
        except Exception:
            logger.exception(
                "撤回业务包 %s/%s@%s 的本地缓存失败", record.tenant_id, record.name, record.version
            )

    async def activate(self, principal: Principal, name: str, version: str) -> None:
        """把某个已发布版本设为生效版本（前进或回滚）。"""
        tenant_id = principal.require_tenant_admin()
        previous = await self._store.get_active_version(tenant_id, name)
        await self._store.set_active_version(tenant_id, name, version, at=self._now())
        await self._audit.control(
            principal,
            tenant_id,
            "package_activated",
            {"name": name, "version": version, "previous": previous},
        )

    async def list_versions(
        self, tenant_id: str, name: str | None = None
    ) -> list[PackageVersionRecord]:
        """列出已发布版本，同名的按版本号从低到高。"""
        records = await self._store.list_package_versions(tenant_id, name)
        return sorted(records, key=lambda record: (record.name, version_key(record.version)))

    async def active_version(self, tenant_id: str, name: str) -> str | None:
        """取生效版本号。"""
        return await self._store.get_active_version(tenant_id, name)

    async def resolve(
        self, tenant_id: str, name: str, version: str | None = None
    ) -> ResolvedPackage:
        """解析出要运行的版本。不指定版本时取生效版本。

        本实例缓存里没有这个版本（或缓存已损坏）时，从对象存储取归档重建。

        Raises:
            NotFoundError: 包不存在、版本不存在，或没有指定版本且尚无生效版本。
            PackageInvalidError: 对象存储里的归档与登记的指纹不符。
            RuntimeUnavailableError: 版本已登记，归档却不在对象存储里。
        """
        chosen = version or await self._store.get_active_version(tenant_id, name)
        if chosen is None:
            raise NotFoundError(f"业务包 {name!r} 不存在，或尚未设置生效版本")
        record = await self._store.get_package_version(tenant_id, name, chosen)
        if record is None:
            raise NotFoundError(f"业务包 {name!r} 没有版本 {chosen!r}")
        root = await self._cache.ensure(record)
        return ResolvedPackage(
            record=record, manifest=parse_manifest(record.manifest_yaml), root=root
        )


__all__ = ["ARCHIVE_CONTENT_TYPE", "PackageRegistry", "ResolvedPackage", "archive_key"]

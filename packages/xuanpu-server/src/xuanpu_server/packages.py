"""业务包登记处：发布、切换生效版本、按租户解析。

发布即不可变：同名同版本不能再次发布，改动必须发新版本。回滚就是把生效版本指回旧版本。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import taifeng
from xuanpu_spec import (
    ArchiveError,
    PackageManifest,
    archive_digest,
    parse_manifest,
    unpack_package,
    validate_package,
    version_key,
)

from xuanpu_server.controlplane.models import PackageVersionRecord
from xuanpu_server.errors import ConflictError, NotFoundError, PackageInvalidError
from xuanpu_server.ids import new_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.tenancy import Principal


@dataclass(frozen=True)
class ResolvedPackage:
    """一个可以拿去运行的业务包版本。"""

    record: PackageVersionRecord
    manifest: PackageManifest

    @property
    def root(self) -> Path:
        """解包后的目录。"""
        return Path(self.record.location)

    @property
    def skills_dir(self) -> Path:
        """skill 目录。"""
        return self.root / self.manifest.spec.skills.dir


def _unpack_and_validate(archive: bytes, staging: Path) -> PackageManifest:
    """解包到暂存目录并按规范校验。阻塞调用，放在线程里跑。"""
    try:
        unpack_package(archive, staging)
    except ArchiveError as exc:
        raise PackageInvalidError(f"业务包归档不合法：{exc}") from exc
    report = validate_package(staging)
    if not report.ok or report.manifest is None:
        raise PackageInvalidError("业务包不符合规范", report.errors)
    return report.manifest


class PackageRegistry:
    """业务包登记处。"""

    def __init__(
        self,
        store: ControlPlaneStore,
        audit: AuditLog,
        *,
        data_dir: Path,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 控制面存储。
            audit: 审计写入口。
            data_dir: 平台数据目录；业务包解到其下的 ``packages/``。
            now: 取当前时间的函数，测试时可替换。
        """
        self._store = store
        self._audit = audit
        self._root = data_dir / "packages"
        self._now = now

    def _final_dir(self, tenant_id: str, name: str, version: str) -> Path:
        """某个版本解包后的位置。三段都已按字符集校验过，不会逃出数据目录。"""
        return self._root / tenant_id / name / version

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
        staging = self._root / ".staging" / new_id("stg")
        try:
            manifest = await asyncio.to_thread(_unpack_and_validate, archive, staging)
            await self._check_loadable(manifest, staging)
            record = await self._commit(principal, tenant_id, manifest, archive, staging)
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, True)
        await self._audit.control(
            principal,
            tenant_id,
            "package_published",
            {"name": record.name, "version": record.version, "digest": record.digest},
        )
        if activate:
            await self.activate(principal, record.name, record.version)
        return record

    async def _commit(
        self,
        principal: Principal,
        tenant_id: str,
        manifest: PackageManifest,
        archive: bytes,
        staging: Path,
    ) -> PackageVersionRecord:
        """把暂存目录转正并登记。登记失败时撤回目录，不留下没有记录的包。"""
        final = self._final_dir(tenant_id, manifest.name, manifest.version)
        if await self._store.get_package_version(tenant_id, manifest.name, manifest.version):
            raise ConflictError(f"业务包 {manifest.name} 的版本 {manifest.version} 已发布")

        def _move() -> None:
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                # 目录在、记录不在：上一次发布登记失败后留下的，可以安全覆盖
                shutil.rmtree(final)
            staging.rename(final)

        await asyncio.to_thread(_move)
        record = PackageVersionRecord(
            tenant_id=tenant_id,
            name=manifest.name,
            version=manifest.version,
            digest=archive_digest(archive),
            manifest_yaml=(final / "xuanpu.yaml").read_text(encoding="utf-8"),
            location=str(final),
            published_at=self._now(),
            published_by=principal.actor,
        )
        try:
            await self._store.add_package_version(record)
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, final, True)
            raise
        return record

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

        Raises:
            NotFoundError: 包不存在、版本不存在，或没有指定版本且尚无生效版本。
        """
        chosen = version or await self._store.get_active_version(tenant_id, name)
        if chosen is None:
            raise NotFoundError(f"业务包 {name!r} 不存在，或尚未设置生效版本")
        record = await self._store.get_package_version(tenant_id, name, chosen)
        if record is None:
            raise NotFoundError(f"业务包 {name!r} 没有版本 {chosen!r}")
        return ResolvedPackage(record=record, manifest=parse_manifest(record.manifest_yaml))


__all__ = ["PackageRegistry", "ResolvedPackage"]

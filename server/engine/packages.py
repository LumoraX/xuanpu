"""业务包登记处：发布、切换生效版本、按租户解析。

发布即不可变：同名同版本不能再次发布，改动必须发新版本。回滚就是把生效版本指回旧版本。

归档的正本放在对象存储里，键是 ``tenants/<租户>/packages/<包名>/<版本>/<指纹>.zip``（指纹
只取 sha256 的十六进制部分），登记在 ``PackageVersionRecord.location``。键里带指纹，不同内容
永远落在不同的对象上：多个实例并发发布同一版本时谁也覆盖不了谁，同内容重试写同一个键无害。
各实例不共享数据目录：用到某个版本时由 ``PackageCache`` 从对象存储取归档、核对指纹、解到
本机缓存 ``<数据目录>/cache/packages/``。

发布的顺序是：写归档 → 登记 → 把校验过的暂存目录转正进本机缓存。缓存只是个加速，转正失败
不影响发布结果。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import taifeng

from server.core.controlplane.models import PackageVersionRecord
from server.core.errors import (
    ConflictError,
    NotFoundError,
    PackageInvalidError,
    RuntimeUnavailableError,
)
from server.engine.package_cache import (
    PackageCache,
    archive_key,
    run_to_completion,
    unpack_archive,
)
from spec import (
    MANIFEST_FILENAME,
    PackageManifest,
    archive_digest,
    display_name_for,
    parse_manifest,
    validate_package,
    version_key,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from server.core.audit import AuditLog
    from server.core.controlplane.store import ControlPlaneStore
    from server.core.objectstore import ObjectStore
    from server.core.tenancy import Principal
    from spec.localization import DisplayNameValue

logger = logging.getLogger(__name__)
_LABEL_CACHE = 512

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
            data_dir: 平台数据目录，必须归本进程独占。业务包缓存在其下的 ``cache/packages/``，
                发布与重建时的暂存目录在 ``cache/.staging/``——两者同在 ``cache/`` 下，保证在
                同一个文件系统上，暂存目录转正时改名是原子的。
            objects: 对象存储，业务包归档的正本放在这里。
            now: 取当前时间的函数，测试时可替换。
        """
        self._store = store
        self._labels: dict[tuple[str, str, str], PackageLabels] = {}
        self._audit = audit
        self._objects = objects
        self._cache = PackageCache(
            objects, data_dir / "cache" / "packages", staging=data_dir / "cache" / ".staging"
        )
        self._now = now
        # 由平台装配注入：判断包是否已纳管为能力（R4-B1）
        self.managed_guard: Callable[[str, str], Awaitable[bool]] | None = None
        self._publishing: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def start(self) -> None:
        """启动时清掉上次进程留下的暂存目录残留。前提：数据目录归本进程独占。"""
        await self._cache.clear_staging()

    async def _check_loadable(
        self, tenant_id: str, manifest: PackageManifest, staging: Path
    ) -> None:
        """交给内核实际加载一遍 skill：支持分类嵌套目录与同租户共享技能库引用。"""
        import tempfile
        from pathlib import Path

        from server.engine.runtime.skill_assembly import (
            materialize_skills,
            resolve_libraries,
        )

        libraries = (
            await resolve_libraries(self, tenant_id, manifest.spec.skills.uses)
            if manifest.spec.skills.uses
            else []
        )
        with tempfile.TemporaryDirectory(prefix="xuanpu-skill-check-") as temp_dir:
            assembled = await asyncio.to_thread(
                materialize_skills,
                staging / manifest.spec.skills.dir,
                Path(temp_dir) / "skills",
                local_package=manifest.name,
                local_connectors=manifest.spec.connectors,
                libraries=libraries,
            )
            try:
                registry = await taifeng.FilesystemSkillRegistry.load(assembled.staged_dir)
            except Exception as exc:  # noqa: BLE001 —— 内核的校验异常不在稳定层，只能按基类接
                raise PackageInvalidError(f"skill 加载失败：{exc}") from exc
            snapshot = registry.snapshot()
        missing = [entry for entry in manifest.spec.skills.entries if snapshot.get(entry) is None]
        if missing:
            raise PackageInvalidError(
                "入口 skill 未能加载", tuple(f"入口 skill {entry!r}" for entry in missing)
            )
        missing_exports = [
            name for name in manifest.spec.skills.exports if snapshot.get(name) is None
        ]
        if missing_exports:
            raise PackageInvalidError(
                "导出 skill 未能加载", tuple(f"导出 skill {name!r}" for name in missing_exports)
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
            RuntimeUnavailableError: 写归档时对象存储出错（可重试）。
        """
        tenant_id = principal.require_tenant_admin()
        async with self._cache.staging() as staging:
            manifest, manifest_yaml = await run_to_completion(
                _unpack_and_validate, archive, staging
            )
            await self._check_loadable(tenant_id, manifest, staging)
            if activate:
                # 纳管包不能借「发布并激活」绕过评测与发布门禁（R4-B1）；写归档之前就拒绝
                await self._guard_activation(tenant_id, manifest.name)
            digest = await asyncio.to_thread(archive_digest, archive)
            record = PackageVersionRecord(
                tenant_id=tenant_id,
                name=manifest.name,
                version=manifest.version,
                digest=digest,
                manifest_yaml=manifest_yaml,
                location=archive_key(tenant_id, manifest.name, manifest.version, digest),
                published_at=self._now(),
                published_by=principal.actor,
            )
            await self._commit(record, archive)
            await self._adopt(record, staging)
        await self._audit.control(
            principal,
            tenant_id,
            "package_published",
            {"name": record.name, "version": record.version, "digest": record.digest},
        )
        if activate:
            await self.activate(principal, record.name, record.version)
        return record

    async def _commit(self, record: PackageVersionRecord, archive: bytes) -> None:
        """查重 → 归档写进对象存储 → 登记。

        本实例内同一版本的发布串行，查重到登记之间不会被自己人插队。写归档或登记失败都撤回
        本次写入的对象，不留下没有记录的归档。

        Raises:
            ConflictError: 同名同版本已经发布过。
            RuntimeUnavailableError: 对象存储出错（可重试）。
        """
        key = (record.tenant_id, record.name, record.version)
        async with self._publishing.setdefault(key, asyncio.Lock()):
            if await self._store.get_package_version(*key):
                raise ConflictError(f"业务包 {record.name} 的版本 {record.version} 已发布")
            try:
                await self._put_archive(record, archive)
                await self._store.add_package_version(record)
            except BaseException:
                await self._rollback(record)
                raise

    async def _put_archive(self, record: PackageVersionRecord, archive: bytes) -> None:
        """把归档写进对象存储。对象存储出错转成 503（可重试），原异常挂在 ``__cause__`` 上。"""
        try:
            await self._objects.put(record.location, archive, content_type=ARCHIVE_CONTENT_TYPE)
        except Exception as exc:
            logger.exception("业务包归档 %s 写入对象存储失败", record.location)
            raise RuntimeUnavailableError("对象存储暂时不可用，业务包没有发布，请稍后重试") from exc

    async def _adopt(self, record: PackageVersionRecord, staging: Path) -> None:
        """已登记的版本把校验过的暂存目录转正进本机缓存。

        失败不影响发布结果：记录已登记、归档在对象存储里，缓存下次用到时重建。只记 warning。
        """
        try:
            await self._cache.adopt(record, staging)
        except Exception:
            logger.warning(
                "业务包 %s/%s@%s 已发布，但转正进本机缓存失败；下次用到时从对象存储重建",
                record.tenant_id, record.name, record.version,
                exc_info=True,
            )  # fmt: skip

    async def _rollback(self, record: PackageVersionRecord) -> None:
        """写归档或登记失败后撤回本次写入的对象。清理失败只记日志，不盖掉原始异常。

        这时还没有转正进本机缓存，缓存不用管。先查控制面里这个版本现在的记录，再决定删不删：

        - 记录不存在，或存在但 ``location`` 不是本次的键（别的实例以不同内容抢先登记了）：
          删掉本次写入的对象——它没有记录指向，留着只是垃圾；
        - 记录存在且 ``location`` 就是本次的键（提交其实成功了，或别的实例以相同内容抢先
          登记）：对象有人在用，不删。

        查不了记录时不删：留下的对象没有记录指向，不影响正确性。

        已知窗口：查记录与删对象之间不是原子的。若本实例查到「没有记录」之后、删除之前，另一个
        实例恰好以相同内容（同一个键）登记成功，本实例会删掉它在用的对象，之后解析该版本报 503。
        彻底解决要靠对象存储的条件删除，或改为按记录做垃圾回收（只删没有记录指向的对象）。
        """
        try:
            registered = await self._store.get_package_version(
                record.tenant_id, record.name, record.version
            )
        except Exception:
            logger.exception(
                "登记失败后无法确认业务包 %s/%s@%s 的登记情况，归档 %s 留在对象存储里",
                record.tenant_id, record.name, record.version, record.location,
            )  # fmt: skip
            return
        if registered is not None and registered.location == record.location:
            return
        try:
            await self._objects.delete(record.location)
        except Exception:
            logger.exception("撤回业务包归档 %s 失败", record.location)

    async def activate(self, principal: Principal, name: str, version: str) -> None:
        """把某个已发布版本设为生效版本（前进或回滚）。纳管包 409，须走能力发布流程。"""
        tenant_id = principal.require_tenant_admin()
        await self._guard_activation(tenant_id, name)
        previous = await self._store.get_active_version(tenant_id, name)
        await self._store.set_active_version(tenant_id, name, version, at=self._now())
        await self._audit.control(
            principal,
            tenant_id,
            "package_activated",
            {"name": name, "version": version, "previous": previous},
        )

    async def _guard_activation(self, tenant_id: str, name: str) -> None:
        """已纳管为能力的包不走旧的激活入口（未纳管的包保持原行为）。"""
        if self.managed_guard is not None and await self.managed_guard(tenant_id, name):
            raise ConflictError("该业务包已纳管为能力，请通过能力发布流程上线，不能直接激活")

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
            RuntimeUnavailableError: 归档不在对象存储里、对象存储出错，或记录本身异常。
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

    async def labels(self, tenant_id: str, name: str, version: str) -> PackageLabels | None:
        """给员工看的名字：匠人显示名与工具显示名（工具按运行时名字 ``mcp__``/``api__`` 索引）。

        版本发布后不可变，按（租户、包、版本）缓存；版本不存在返回 None，由调用方用技术名。
        """
        key = (tenant_id, name, version)
        cached = self._labels.get(key)
        if cached is not None:
            return cached
        record = await self._store.get_package_version(tenant_id, name, version)
        if record is None:
            return None
        manifest = parse_manifest(record.manifest_yaml)
        tools: dict[str, DisplayNameValue] = {
            f"{'api' if c.transport == 'http' else 'mcp'}__{c.name}__{t.name}": t.display_name
            for c in manifest.spec.connectors for t in c.tools if t.display_name
        }
        labels = PackageLabels(display_name=manifest.metadata.display_name, tools=tools)
        if len(self._labels) >= _LABEL_CACHE:
            self._labels.pop(next(iter(self._labels)))
        self._labels[key] = labels
        return labels

    async def declared_secrets(
        self, tenant_id: str, *, locale: str = "zh-CN"
    ) -> list[dict[str, object]]:
        """汇总租户当前生效（或最新已发布）业务包声明需要的连接器密钥及其使用方。"""
        records = await self.list_versions(tenant_id)
        by_name: dict[str, list[PackageVersionRecord]] = {}
        for record in records:
            by_name.setdefault(record.name, []).append(record)
        usage: dict[str, list[dict[str, str]]] = {}
        for pkg_name in sorted(by_name):
            active = await self._store.get_active_version(tenant_id, pkg_name)
            chosen = next((r for r in by_name[pkg_name] if r.version == active), by_name[pkg_name][-1])
            manifest = parse_manifest(chosen.manifest_yaml)
            pkg_label = display_name_for(manifest.metadata.display_name, locale) or manifest.name
            for connector in manifest.spec.connectors:
                for secret_name in connector.secret_names:
                    usage.setdefault(secret_name, []).append({
                        "package": manifest.name,
                        "display_name": pkg_label,
                        "connector": connector.name,
                    })
        return [
            {"name": secret_name, "used_by": usage[secret_name]}
            for secret_name in sorted(usage)
        ]


@dataclass(frozen=True)
class PackageLabels:
    """一个包版本给员工看的名字。"""

    display_name: DisplayNameValue | None
    tools: dict[str, DisplayNameValue]


__all__ = ["ARCHIVE_CONTENT_TYPE", "PackageLabels", "PackageRegistry", "ResolvedPackage"]

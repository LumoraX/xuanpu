"""能力市场服务：统一浏览匠人市场与 Skill 市场，并支持带依赖解析的一键安装。"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from server.core.errors import NotFoundError, PackageInvalidError
from server.domains.marketplace.config import MarketplaceConfig
from server.domains.marketplace.models import CatalogItem, CatalogSkill, MarketplaceKind
from spec import (
    MANIFEST_FILENAME,
    PackageManifest,
    display_name_for,
    parse_manifest,
    version_key,
)
from spec.archive import pack_package, unpack_package
from spec.skills import scan_skills

if TYPE_CHECKING:
    from server.core.secrets import SecretVault
    from server.core.tenancy import Principal
    from server.engine.packages import PackageRegistry

logger = logging.getLogger(__name__)


def _extract_item(
    root: Path,
    *,
    source_path: Path,
    is_archive: bool,
    locale: str,
) -> CatalogItem | None:
    """从解包后的包目录提取市场元数据；清单非法时跳过并记录告警。"""
    manifest_file = root / MANIFEST_FILENAME
    if not manifest_file.is_file():
        return None
    try:
        manifest = parse_manifest(manifest_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 市场扫描单个损坏包不影响其余目录项
        logger.warning("跳过无法解析的市场能力包 %s：%s", source_path, exc)
        return None
    return _item_from_manifest(
        manifest,
        skills_dir=root / manifest.spec.skills.dir,
        source_path=source_path,
        is_archive=is_archive,
        locale=locale,
        source="catalog",
    )


def _item_from_manifest(
    manifest: PackageManifest,
    *,
    skills_dir: Path | None,
    source_path: Path | None,
    is_archive: bool,
    locale: str,
    source: str,
) -> CatalogItem:
    """由清单与可选技能目录构造市场目录项。"""
    kind: MarketplaceKind = "skill_library" if manifest.is_library else "artisan"
    exported_set = set(manifest.spec.skills.exports)
    entry_set = set(manifest.spec.skills.entries)
    skills: list[CatalogSkill] = []
    categories: set[str] = set()
    tags: set[str] = set()
    if skills_dir is not None and skills_dir.is_dir():
        for summary in scan_skills(skills_dir):
            if summary.problem is not None:
                continue
            skill_label = display_name_for(summary.display_name, locale) or summary.skill_id
            if summary.category:
                categories.add(summary.category)
            tags.update(summary.tags)
            skills.append(
                CatalogSkill(
                    skill_id=summary.skill_id,
                    display_name=skill_label,
                    category=summary.category,
                    tags=summary.tags,
                    description=summary.description,
                    exported=summary.skill_id in exported_set,
                    entry=summary.skill_id in entry_set,
                )
            )
    secrets: list[str] = []
    for conn in manifest.spec.connectors:
        for secret_name in conn.secret_names:
            if secret_name not in secrets:
                secrets.append(secret_name)
    uses = tuple(dict.fromkeys(use.package for use in manifest.spec.skills.uses))
    connectors = tuple(conn.name for conn in manifest.spec.connectors)
    pkg_label = display_name_for(manifest.metadata.display_name, locale) or manifest.name
    return CatalogItem(
        name=manifest.name,
        version=manifest.version,
        kind=kind,
        display_name=pkg_label,
        description=manifest.metadata.description,
        categories=tuple(sorted(categories)),
        tags=tuple(sorted(tags)),
        skills=tuple(skills),
        uses=uses,
        required_secrets=tuple(secrets),
        connectors=connectors,
        source_path=source_path,
        is_archive=is_archive,
        source="catalog" if source == "catalog" else "tenant",
    )


def _scan_dirs_sync(catalog_dirs: tuple[Path, ...], locale: str) -> dict[str, CatalogItem]:
    """同步扫描配置的全部市场目录（在工作线程调用），支持平铺目录与分组目录（skills/plugins/artisans）。"""
    items: dict[str, CatalogItem] = {}

    def record(item: CatalogItem | None) -> None:
        if item is None:
            return
        existing = items.get(item.name)
        if existing is None or version_key(item.version) > version_key(existing.version):
            items[item.name] = item

    def is_package_dir(path: Path) -> bool:
        """区分业务包清单（apiVersion: xuanpu/v1）与同名服务配置模板（如 examples/xuanpu.yaml）。"""
        manifest = path / MANIFEST_FILENAME
        if not manifest.is_file():
            return False
        try:
            head = manifest.read_text(encoding="utf-8", errors="ignore")[:512]
        except OSError:
            return False
        return "apiVersion:" in head

    def scan_entry(entry: Path, *, allow_group: bool) -> None:
        if entry.name.startswith("."):
            return
        if entry.is_dir():
            if is_package_dir(entry):
                record(_extract_item(entry, source_path=entry, is_archive=False, locale=locale))
            elif allow_group:
                # 兼容独立生态仓的分组结构（如 lumorax-catalog/skills/*、artisans/*、plugins/*）
                for child in sorted(entry.iterdir()):
                    scan_entry(child, allow_group=False)
        elif entry.is_file() and entry.suffix == ".zip":
            with tempfile.TemporaryDirectory(prefix="xuanpu-market-") as tmp:
                try:
                    unpack_package(entry.read_bytes(), Path(tmp))
                except Exception as exc:  # noqa: BLE001 —— 跳过损坏归档
                    logger.warning("跳过无法解包的市场归档 %s：%s", entry, exc)
                    return
                record(
                    _extract_item(
                        Path(tmp), source_path=entry, is_archive=True, locale=locale
                    )
                )

    for directory in catalog_dirs:
        if not directory.is_dir():
            continue
        if is_package_dir(directory):
            scan_entry(directory, allow_group=False)
            continue
        for entry in sorted(directory.iterdir()):
            scan_entry(entry, allow_group=True)
    return _resolve_transitive_secrets(items)


def _resolve_transitive_secrets(items: dict[str, CatalogItem]) -> dict[str, CatalogItem]:
    """把所依赖技能库声明的密钥合并到匠人目录项，便于市场页提前提示待配凭据。"""
    resolved: dict[str, CatalogItem] = {}

    def collect_secrets(name: str, seen: set[str]) -> list[str]:
        if name in seen or name not in items:
            return []
        seen.add(name)
        current = items[name]
        names = list(current.required_secrets)
        for dep in current.uses:
            for secret in collect_secrets(dep, seen):
                if secret not in names:
                    names.append(secret)
        return names

    for name, item in items.items():
        merged = tuple(collect_secrets(name, set()))
        if merged == item.required_secrets:
            resolved[name] = item
        else:
            resolved[name] = CatalogItem(
                name=item.name,
                version=item.version,
                kind=item.kind,
                display_name=item.display_name,
                description=item.description,
                categories=item.categories,
                tags=item.tags,
                skills=item.skills,
                uses=item.uses,
                required_secrets=merged,
                connectors=item.connectors,
                source_path=item.source_path,
                is_archive=item.is_archive,
                source=item.source,
            )
    return resolved


class MarketplaceService:
    """能力市场服务：聚合匠人市场与 Skill 市场，并提供带依赖解析的一键安装。"""

    def __init__(
        self,
        packages: PackageRegistry,
        vault: SecretVault,
        config: MarketplaceConfig | None = None,
    ) -> None:
        """注入业务包登记处、租户密钥库与市场目录配置。"""
        self._packages = packages
        self._vault = vault
        self._config = config or MarketplaceConfig()

    @property
    def catalog_dirs(self) -> tuple[Path, ...]:
        """当前配置的市场目录列表。"""
        return self._config.catalog_dirs

    async def _scan(self, *, locale: str = "zh-CN") -> dict[str, CatalogItem]:
        """异步扫描市场目录。"""
        if not self._config.catalog_dirs:
            return {}
        return await asyncio.to_thread(_scan_dirs_sync, self._config.catalog_dirs, locale)

    async def list_items(
        self,
        who: Principal,
        *,
        kind: str = "all",
        category: str | None = None,
        query: str | None = None,
        locale: str = "zh-CN",
    ) -> dict[str, Any]:
        """列出租户可见的市场能力（匠人 + 技能库），附带安装状态与密钥就绪度。"""
        tenant_id = who.require_tenant()
        catalog = await self._scan(locale=locale)
        versions = await self._packages.list_versions(tenant_id)
        configured = set(await self._vault.names(tenant_id))

        by_pkg: dict[str, list[Any]] = {}
        for rec in versions:
            by_pkg.setdefault(rec.name, []).append(rec)
        active_map: dict[str, str | None] = {}
        for pkg_name in by_pkg:
            active_map[pkg_name] = await self._packages.active_version(tenant_id, pkg_name)

        # 把租户自己已发布但不在官方市场目录里的包也纳入视图，便于统一看全貌
        merged: dict[str, CatalogItem] = dict(catalog)
        for pkg_name, records in by_pkg.items():
            if pkg_name in merged:
                continue
            active = active_map.get(pkg_name)
            chosen = next((r for r in records if r.version == active), records[-1])
            manifest = parse_manifest(chosen.manifest_yaml)
            merged[pkg_name] = _item_from_manifest(
                manifest,
                skills_dir=None,
                source_path=None,
                is_archive=False,
                locale=locale,
                source="tenant",
            )

        all_categories: set[str] = set()
        counts = {"all": 0, "artisan": 0, "skill_library": 0}
        views: list[dict[str, Any]] = []
        q = (query or "").strip().lower()
        cat = (category or "").strip()

        for pkg_name in sorted(merged):
            item = merged[pkg_name]
            all_categories.update(item.categories)
            counts["all"] += 1
            counts[item.kind] += 1
            if kind in ("artisan", "skill_library") and item.kind != kind:
                continue
            if cat and not any(c == cat or c.startswith(cat + "/") for c in item.categories):
                continue
            if q and not _matches_query(item, q):
                continue
            records = by_pkg.get(pkg_name, [])
            active = active_map.get(pkg_name)
            latest_installed = records[-1].version if records else None
            installed_ver = active or latest_installed
            installed = bool(records)
            upgrade_available = (
                item.source == "catalog"
                and installed_ver is not None
                and version_key(item.version) > version_key(installed_ver)
            )
            views.append({
                "name": item.name,
                "version": item.version,
                "kind": item.kind,
                "display_name": item.display_name,
                "description": item.description,
                "categories": list(item.categories),
                "tags": list(item.tags),
                "skills": [s.to_dict() for s in item.skills],
                "uses": list(item.uses),
                "connectors": list(item.connectors),
                "source": item.source,
                "installed": installed,
                "active": active is not None,
                "installed_version": installed_ver,
                "upgrade_available": upgrade_available,
                "secrets": [
                    {"name": s, "configured": s in configured}
                    for s in item.required_secrets
                ],
            })

        return {
            "items": views,
            "categories": sorted(all_categories),
            "counts": counts,
        }

    async def install(
        self,
        who: Principal,
        name: str,
        *,
        activate: bool = True,
        locale: str = "zh-CN",
    ) -> dict[str, Any]:
        """一键安装市场中的匠人或技能库；若声明了 ``skills.uses`` 依赖，自动按拓扑序先安装依赖库。"""
        tenant_id = who.require_tenant_admin()
        catalog = await self._scan(locale=locale)
        if name not in catalog:
            raise NotFoundError(f"能力市场中找不到 {name!r}")

        order = _install_order(catalog, name)
        installed_results: list[dict[str, Any]] = []
        for pkg_name in order:
            item = catalog[pkg_name]
            status = await self._install_one(who, tenant_id, item, activate=activate)
            installed_results.append({
                "name": item.name,
                "display_name": item.display_name,
                "version": item.version,
                "kind": item.kind,
                "status": status,
            })

        configured = set(await self._vault.names(tenant_id))
        target = catalog[name]
        missing = [s for s in target.required_secrets if s not in configured]
        return {
            "installed": installed_results,
            "missing_secrets": missing,
        }

    async def _install_one(
        self,
        who: Principal,
        tenant_id: str,
        item: CatalogItem,
        *,
        activate: bool,
    ) -> str:
        """把单个目录项发布到当前租户；同版本已存在时幂等处理。"""
        existing = await self._packages.list_versions(tenant_id, item.name)
        already = any(rec.version == item.version for rec in existing)
        if already:
            if activate and await self._packages.active_version(tenant_id, item.name) != item.version:
                await self._packages.activate(who, item.name, item.version)
                return "activated"
            return "already_installed"

        if item.source_path is None:
            raise NotFoundError(f"能力 {item.name!r} 缺少可安装的来源归档")
        if item.is_archive:
            archive = await asyncio.to_thread(item.source_path.read_bytes)
        else:
            archive = await asyncio.to_thread(pack_package, item.source_path)
        await self._packages.publish(who, archive, activate=activate)
        return "installed"


def _matches_query(item: CatalogItem, q: str) -> bool:
    """按名称、描述、分类、标签与子技能匹配关键词。"""
    haystack = " ".join([
        item.name,
        item.display_name,
        item.description,
        *item.categories,
        *item.tags,
        *item.uses,
        *(s.skill_id for s in item.skills),
        *(s.display_name for s in item.skills),
        *(s.description for s in item.skills),
    ]).lower()
    return q in haystack


def _install_order(catalog: dict[str, CatalogItem], root_name: str) -> list[str]:
    """拓扑展开 ``uses`` 依赖，保证技能库先于引用它的匠人安装；检测循环依赖。"""
    order: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(pkg_name: str) -> None:
        if pkg_name in visited:
            return
        if pkg_name in visiting:
            raise PackageInvalidError(f"能力市场依赖存在循环引用：{pkg_name}")
        item = catalog.get(pkg_name)
        if item is None:
            return
        visiting.add(pkg_name)
        for dep in item.uses:
            visit(dep)
        visiting.remove(pkg_name)
        visited.add(pkg_name)
        order.append(pkg_name)

    visit(root_name)
    return order


__all__ = ["MarketplaceConfig", "MarketplaceService"]

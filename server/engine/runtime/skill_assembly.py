"""技能目录装配与分类渐进检索：支持分类嵌套目录、跨包共享技能库与按需召回。

设计要点：
- 内核 ``taifeng`` 只扫描单层 ``skills/<id>/SKILL.md``（红线 2/3：不改动内核、只用 ``taifeng.__all__``）。
- 平台在加载与组装运行时前，把按分类嵌套的本地技能目录及 ``skills.uses`` 引用的同租户通用技能库
  物化为内核可直接加载的扁平视图目录，并保留每个技能的分类、标签与来源元数据。
- ``CategorizedSkillRecall`` 实现稳定层 ``taifeng.SkillRecall`` 协议，把分类与标签纳入 BM25
  检索语料，让模型在 ``child_recall: deferred`` 模式下通过 ``search_skills`` 按分类或关键词渐进发现技能。
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

import taifeng

from server.core.errors import NotFoundError, PackageInvalidError
from spec.localization import display_name_for
from spec.skills import SkillSummary, scan_skills
from spec.validate import category_matches, split_tool_name

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from server.engine.packages import PackageRegistry, ResolvedPackage
    from spec.manifest import ConnectorSpec, SkillLibraryUse


@dataclass(frozen=True)
class SkillCatalogEntry:
    """单个已装配 skill 的分类与检索元数据。"""

    skill_id: str
    category: str | None
    tags: tuple[str, ...]
    display_name: str | None
    description: str
    source_package: str
    rel_path: str

    def recall_text(self, original_description: str) -> str:
        """拼出供关键词召回匹配的语料，纳入分类、标签与显示名。"""
        parts: list[str] = []
        if self.category:
            parts.append(f"分类:{self.category} {self.category.replace('/', ' ')}")
        if self.tags:
            parts.append("标签:" + " ".join(self.tags))
        if self.display_name:
            parts.append(self.display_name)
        parts.append(original_description)
        return " | ".join(parts)

    def annotated_description(self, original_description: str) -> str:
        """返回给模型浏览的精简描述：带分类前缀，避免把正文提前塞进上下文。"""
        if self.category and not original_description.startswith(f"[{self.category}]"):
            return f"[{self.category}] {original_description}"
        return original_description


@dataclass(frozen=True)
class AssembledSkills:
    """一次运行时装配得到的技能视图与附加连接器。

    Attributes:
        staged_dir: 传给 ``taifeng.EnginePool.create`` 的技能目录（扁平布局）。
        entries: 按 ``skill_id`` 索引的分类与标签目录，供 ``CategorizedSkillRecall`` 使用。
        extra_connectors: 从共享技能库引入的 ``(连接器声明, 技能库根目录)`` 列表。
        needs_cleanup: ``staged_dir`` 是否由本次装配物化生成。
    """

    staged_dir: Path
    entries: dict[str, SkillCatalogEntry]
    extra_connectors: tuple[tuple[ConnectorSpec, Path], ...] = ()
    needs_cleanup: bool = False


class CategorizedSkillRecall:
    """支持分类与标签感知的技能召回后端（实现 ``taifeng.SkillRecall`` 协议）。"""

    def __init__(self, catalog: Mapping[str, SkillCatalogEntry]) -> None:
        """注入技能目录元数据，底层复用内核确定性 ``KeywordSkillRecall``。"""
        self._catalog = dict(catalog)
        self._inner = taifeng.KeywordSkillRecall()

    async def recall(
        self,
        query: str,
        pool: Sequence[taifeng.RecallEntry],
        *,
        top_k: int,
        cancel: taifeng.CancellationToken,
    ) -> list[taifeng.SkillCandidate]:
        """在内核限定的白名单池内按分类、标签与描述检索 top_k 个候选。"""
        originals = {entry.skill_id: entry.description for entry in pool}
        enriched: list[taifeng.RecallEntry] = []
        for entry in pool:
            meta = self._catalog.get(entry.skill_id)
            text = meta.recall_text(entry.description) if meta is not None else entry.description
            enriched.append(taifeng.RecallEntry(skill_id=entry.skill_id, description=text))
        candidates = await self._inner.recall(query, enriched, top_k=top_k, cancel=cancel)
        result: list[taifeng.SkillCandidate] = []
        for item in candidates:
            orig = originals.get(item.skill_id, item.description)
            meta = self._catalog.get(item.skill_id)
            desc = meta.annotated_description(orig) if meta is not None else orig
            result.append(
                taifeng.SkillCandidate(
                    skill_id=item.skill_id,
                    description=desc,
                    score=item.score,
                    confidence=item.confidence,
                    matched_snippet=item.matched_snippet,
                )
            )
        return result


def _entry_of(skill: SkillSummary, source_package: str) -> SkillCatalogEntry:
    """把规范层 ``SkillSummary`` 转成运行时检索元数据。"""
    return SkillCatalogEntry(
        skill_id=skill.skill_id,
        category=skill.category,
        tags=skill.tags,
        display_name=display_name_for(skill.display_name, "zh-CN"),
        description=skill.description,
        source_package=source_package,
        rel_path=skill.location,
    )


def _expand_library_skills(
    use: SkillLibraryUse,
    lib_pkg: ResolvedPackage,
) -> tuple[list[SkillSummary], list[ConnectorSpec]]:
    """按 ``uses`` 声明选出技能库导出的技能、其内部依赖子技能，以及所需连接器。"""
    manifest = lib_pkg.manifest
    exported = manifest.spec.skills.exports
    if not exported:
        raise PackageInvalidError(f"引用的技能库 {use.package!r} 没有声明任何 exports")
    scanned = {s.skill_id: s for s in scan_skills(lib_pkg.skills_dir) if s.problem is None}
    for wanted in use.skills:
        if wanted not in exported:
            raise PackageInvalidError(
                f"skills.uses 引用的 skill {wanted!r} 不在技能库 {use.package!r} 的 exports 里"
            )
    roots = [
        scanned[sid]
        for sid in exported
        if sid in scanned
        and (not use.skills or sid in use.skills)
        and category_matches(scanned[sid].category, use.categories)
    ]
    if not roots:
        raise PackageInvalidError(f"从技能库 {use.package!r} 按条件未匹配到任何导出 skill")
    # 沿 child_skills 展开库内私有辅助 skill，确保内核静态引用检查通过
    selected: dict[str, SkillSummary] = {}
    queue = list(roots)
    while queue:
        current = queue.pop(0)
        if current.skill_id in selected:
            continue
        selected[current.skill_id] = current
        for child_id in current.child_skills:
            child = scanned.get(child_id)
            if child is not None and child_id not in selected:
                queue.append(child)
    needed_connectors: set[str] = set()
    for skill in selected.values():
        for tool_name in skill.tool_names:
            parts = split_tool_name(tool_name)
            if parts is not None:
                needed_connectors.add(parts[1])
    connectors = [c for c in manifest.spec.connectors if c.name in needed_connectors]
    return list(selected.values()), connectors


def _copy_skill_dir(source: Path, target: Path) -> None:
    """把单个 skill 目录完整复制到扁平视图下（保留 references/、scripts/ 等附属文件）。"""
    shutil.copytree(source, target, symlinks=False)


def materialize_skills(
    skills_dir: Path,
    destination: Path,
    *,
    local_package: str,
    local_connectors: Sequence[ConnectorSpec] = (),
    libraries: Sequence[tuple[SkillLibraryUse, ResolvedPackage]] = (),
) -> AssembledSkills:
    """装配本地分类技能与共享技能库，生成内核可加载的技能目录与检索目录。

    当业务包没有引用外部技能库且本地技能全为单层平铺目录时，直接复用原目录、零复制开销。
    """
    local_skills = [s for s in scan_skills(skills_dir) if s.problem is None]
    entries = {s.skill_id: _entry_of(s, local_package) for s in local_skills}
    flat_only = not libraries and all(
        "/" not in s.location and s.skill_id == s.directory for s in local_skills
    )
    if flat_only:
        return AssembledSkills(staged_dir=skills_dir, entries=entries, needs_cleanup=False)

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for skill in local_skills:
        _copy_skill_dir(skills_dir / skill.location, destination / skill.skill_id)

    connector_names = {c.name for c in local_connectors}
    extra_connectors: list[tuple[ConnectorSpec, Path]] = []
    for use, lib_pkg in libraries:
        selected_skills, lib_connectors = _expand_library_skills(use, lib_pkg)
        for skill in selected_skills:
            if skill.skill_id in entries:
                raise PackageInvalidError(
                    f"技能库 {use.package!r} 的 skill {skill.skill_id!r} 与已有 skill 重名"
                )
            entries[skill.skill_id] = _entry_of(skill, lib_pkg.record.name)
            _copy_skill_dir(lib_pkg.skills_dir / skill.location, destination / skill.skill_id)
        for connector in lib_connectors:
            if connector.name in connector_names:
                raise PackageInvalidError(
                    f"技能库 {use.package!r} 的连接器 {connector.name!r} 与已有连接器重名"
                )
            connector_names.add(connector.name)
            extra_connectors.append((connector, lib_pkg.root))
    return AssembledSkills(
        staged_dir=destination,
        entries=entries,
        extra_connectors=tuple(extra_connectors),
        needs_cleanup=True,
    )


async def resolve_libraries(
    packages: PackageRegistry,
    tenant_id: str,
    uses: Sequence[SkillLibraryUse],
) -> list[tuple[SkillLibraryUse, ResolvedPackage]]:
    """按租户解析业务包声明引用的全部共享技能库（严格租户隔离）。"""
    resolved: list[tuple[SkillLibraryUse, ResolvedPackage]] = []
    for use in uses:
        try:
            lib_pkg = await packages.resolve(tenant_id, use.package, use.version)
        except NotFoundError as exc:
            raise PackageInvalidError(
                f"引用的共享技能库 {use.package!r} 在当前租户下不存在或尚未生效"
            ) from exc
        resolved.append((use, lib_pkg))
    return resolved


def library_fingerprint(libraries: Sequence[tuple[SkillLibraryUse, ResolvedPackage]]) -> str:
    """计算所引用共享技能库集合的稳定指纹，供运行时缓存键区分库版本升级。"""
    hasher = hashlib.sha256()
    for use, lib_pkg in libraries:
        parts = (
            lib_pkg.record.name,
            lib_pkg.record.version,
            lib_pkg.record.digest,
            ",".join(use.categories),
            ",".join(use.skills),
        )
        hasher.update("|".join(parts).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()[:12]


__all__ = [
    "AssembledSkills",
    "CategorizedSkillRecall",
    "SkillCatalogEntry",
    "library_fingerprint",
    "materialize_skills",
    "resolve_libraries",
]

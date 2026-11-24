"""业务包目录的整体校验：清单本身，以及清单与包内容是否对得上。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from spec.errors import ManifestError
from spec.loader import load_manifest
from spec.manifest import (
    MANIFEST_FILENAME,
    ConnectorSpec,
    PackageManifest,
    SkillLibraryUse,
)
from spec.openapi import ApiDocument, OpenApiError, load_openapi
from spec.skills import scan_skills

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from spec.skills import SkillSummary

MCP_TOOL_PREFIX = "mcp__"
API_TOOL_PREFIX = "api__"
_TOOL_PREFIXES = (MCP_TOOL_PREFIX, API_TOOL_PREFIX)


def connector_tool_name(connector: str, tool: str) -> str:
    """模型看到的 MCP 连接器（``stdio`` 与 ``remote``）工具名。

    HTTP 连接器的工具名见 ``ConnectorSpec.tool_name``。
    """
    return f"{MCP_TOOL_PREFIX}{connector}__{tool}"


def split_tool_name(tool_name: str) -> tuple[str, str, str] | None:
    """把工具名拆成 ``(前缀, 连接器名, 工具名)``；不是连接器工具返回 None。"""
    for prefix in _TOOL_PREFIXES:
        if tool_name.startswith(prefix):
            connector, _, tool = tool_name[len(prefix) :].partition("__")
            return prefix, connector, tool
    return None


@dataclass(frozen=True)
class ValidationReport:
    """校验结果。

    Attributes:
        manifest: 解析出的清单；清单本身不合法时为 None。
        errors: 必须修正的问题，有任何一条则包不可发布。
        warnings: 值得留意但不阻止发布的问题。
    """

    manifest: PackageManifest | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """是否可以发布。"""
        return self.manifest is not None and not self.errors


@dataclass
class _Findings:
    """校验过程中累积的发现。"""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_safe_relative(path: str) -> bool:
    """是否是不会逃出包目录的相对路径。"""
    pure = PurePosixPath(path)
    return not pure.is_absolute() and ".." not in pure.parts and path.strip() != ""


def category_matches(skill_category: str | None, wanted: tuple[str, ...]) -> bool:
    """判断 skill 所属分类是否命中过滤列表（支持精确匹配与父分类前缀匹配）。"""
    if not wanted:
        return True
    if not skill_category:
        return False
    return any(
        skill_category == target or skill_category.startswith(target + "/")
        for target in wanted
    )


def _index_skills(skills: list[SkillSummary], findings: _Findings) -> dict[str, SkillSummary]:
    """检查单个包里的 skill 概要，返回按 skill_id 索引的映射。"""
    by_id: dict[str, SkillSummary] = {}
    for skill in skills:
        if skill.problem is not None:
            findings.errors.append(f"skill 目录 {skill.location!r}：{skill.problem}")
            continue
        if skill.skill_id in by_id:
            findings.errors.append(
                f"skill {skill.skill_id!r} 重复出现在目录 {by_id[skill.skill_id].location!r} "
                f"与 {skill.location!r}"
            )
            continue
        by_id[skill.skill_id] = skill
    return by_id


def _check_skills(
    manifest: PackageManifest, skills: list[SkillSummary], findings: _Findings
) -> dict[str, SkillSummary]:
    """核对 skill 目录、入口、导出项与工具引用。"""
    by_id = _index_skills(skills, findings)
    if not skills:
        findings.errors.append(f"skill 目录 {manifest.spec.skills.dir!r} 不存在或为空")
    for entry in manifest.spec.skills.entries:
        declared_entry = by_id.get(entry)
        if declared_entry is None:
            findings.errors.append(f"入口 skill {entry!r} 在 skill 目录里找不到")
        elif not declared_entry.entry:
            findings.errors.append(f"入口 skill {entry!r} 的 SKILL.md 没有声明 entry: true")
    for exported in manifest.spec.skills.exports:
        if exported not in by_id:
            findings.errors.append(f"导出 skill {exported!r} 在 skill 目录里找不到")
    declared = set(manifest.spec.skills.entries)
    for skill in by_id.values():
        if skill.entry and skill.skill_id not in declared:
            findings.warnings.append(
                f"skill {skill.skill_id!r} 声明了 entry: true，但不在清单的 entries 里，"
                "不会作为会话入口开放"
            )
    for use in manifest.spec.skills.uses:
        if use.package == manifest.name:
            findings.errors.append(f"skills.uses 不能引用业务包自身 {use.package!r}")
    _check_tool_references(manifest, list(by_id.values()), findings)
    return by_id


def _check_tool_references(
    manifest: PackageManifest, skills: list[SkillSummary], findings: _Findings
) -> None:
    """skill 引用的连接器工具必须对应清单里声明的连接器、正确的前缀与白名单。

    前缀由连接器的传输方式决定（``ConnectorSpec.tool_prefix``）：``stdio`` 与 ``remote``
    是 ``mcp__``，``http`` 是 ``api__``。
    """
    connectors = {connector.name: connector for connector in manifest.spec.connectors}
    for skill in skills:
        for tool_name in skill.tool_names:
            parts = split_tool_name(tool_name)
            if parts is None:
                continue
            prefix, connector_name, tool = parts
            connector = connectors.get(connector_name)
            if connector is None or not tool:
                findings.errors.append(
                    f"skill {skill.skill_id!r} 引用了工具 {tool_name!r}，"
                    f"但清单里没有连接器 {connector_name!r}"
                )
            elif connector.tool_prefix != prefix + connector_name + "__":
                findings.errors.append(
                    f"skill {skill.skill_id!r} 引用的工具 {tool_name!r} 前缀与连接器 "
                    f"{connector_name!r} 的传输方式不符（应为 {connector.tool_prefix}…）"
                )
            elif not connector.allows(tool):
                findings.errors.append(
                    f"skill {skill.skill_id!r} 引用了工具 {tool_name!r}，"
                    f"但它不在连接器 {connector_name!r} 的工具白名单里"
                )


def _select_library_exports(
    use: SkillLibraryUse,
    lib_manifest: PackageManifest,
    lib_skills: dict[str, SkillSummary],
    findings: _Findings,
) -> list[SkillSummary]:
    """按 uses 声明从技能库中选出要引入的导出 skill。"""
    exported = set(lib_manifest.spec.skills.exports)
    if not exported:
        findings.errors.append(f"引用的技能库 {use.package!r} 没有声明任何 exports")
        return []
    for wanted_skill in use.skills:
        if wanted_skill not in exported:
            findings.errors.append(
                f"skills.uses 引用的 skill {wanted_skill!r} 不在技能库 {use.package!r} 的 exports 里"
            )
    selected = [
        lib_skills[sid]
        for sid in lib_manifest.spec.skills.exports
        if sid in lib_skills
        and (not use.skills or sid in use.skills)
        and category_matches(lib_skills[sid].category, use.categories)
    ]
    if not selected and not use.skills:
        findings.errors.append(
            f"从技能库 {use.package!r} 按条件未匹配到任何导出 skill"
        )
    return selected


def _check_libraries(
    manifest: PackageManifest,
    local_skills: dict[str, SkillSummary],
    libraries: Mapping[str, Path],
    findings: _Findings,
) -> None:
    """在提供了本地技能库目录映射时，校验 uses 引用、重名冲突与跨包 child_skills。"""
    all_skill_ids = set(local_skills)
    used_connectors = {connector.name for connector in manifest.spec.connectors}
    for use in manifest.spec.skills.uses:
        lib_root = libraries.get(use.package)
        if lib_root is None:
            findings.errors.append(f"校验未提供被引用的技能库目录：{use.package!r}")
            continue
        lib_report = validate_package(lib_root)
        if not lib_report.ok or lib_report.manifest is None:
            findings.errors.append(f"引用的技能库 {use.package!r} 自身校验未通过")
            continue
        lib_manifest = lib_report.manifest
        lib_skills = {s.skill_id: s for s in scan_skills(lib_root / lib_manifest.spec.skills.dir)}
        selected = _select_library_exports(use, lib_manifest, lib_skills, findings)
        lib_connector_names: set[str] = set()
        for skill in selected:
            if skill.skill_id in all_skill_ids:
                findings.errors.append(
                    f"从技能库 {use.package!r} 引入的 skill {skill.skill_id!r} 与已有 skill 重名"
                )
            all_skill_ids.add(skill.skill_id)
            for tool_name in skill.tool_names:
                parts = split_tool_name(tool_name)
                if parts is not None:
                    lib_connector_names.add(parts[1])
        overlap = used_connectors & lib_connector_names
        if overlap:
            findings.errors.append(
                f"技能库 {use.package!r} 的连接器与当前包或其他技能库重名：{', '.join(sorted(overlap))}"
            )
        used_connectors |= lib_connector_names
    for skill in local_skills.values():
        missing = sorted(set(skill.child_skills) - all_skill_ids)
        if missing:
            findings.errors.append(
                f"skill {skill.skill_id!r} 引用的 child_skills 未找到：{', '.join(missing)}"
            )


def _check_http_connector(
    root: Path, connector: ConnectorSpec, findings: _Findings
) -> ApiDocument | None:
    """HTTP 连接器：文档要读得出来，白名单里的 operation 要存在，基地址要有着落。"""
    assert connector.openapi is not None
    if not is_safe_relative(connector.openapi):
        findings.errors.append(f"连接器 {connector.name!r} 的 openapi 必须是包内的相对路径")
        return None
    try:
        document = load_openapi(root / connector.openapi)
    except OpenApiError as exc:
        findings.errors.append(f"连接器 {connector.name!r} 的 OpenAPI 文档：{exc}")
        return None
    for tool in connector.tools:
        if tool.name not in document.operations:
            findings.errors.append(
                f"连接器 {connector.name!r} 的白名单里有 {tool.name!r}，"
                "但 OpenAPI 文档里没有这个 operation"
            )
    if connector.base_url is None and not any(
        server.startswith(("http://", "https://")) for server in document.servers
    ):
        findings.errors.append(
            f"连接器 {connector.name!r} 没有 baseUrl，OpenAPI 文档里也没有绝对地址的 servers"
        )
    return document


def _check_remote_connector(connector: ConnectorSpec, findings: _Findings) -> None:
    """remote 连接器：地址的写法里有两种值得提醒的情形（不阻止发布）。"""
    assert connector.url is not None
    parts = urlsplit(connector.url)
    if parts.scheme == "http" and connector.auth is not None:
        findings.warnings.append(
            f"连接器 {connector.name!r} 的 url 用的是 http://，凭据会明文经过网络；"
            "只宜试用，正式使用请换成 https://"
        )
    if parts.query:
        findings.warnings.append(
            f"连接器 {connector.name!r} 的 url 带查询参数：不要把凭据放在 url 里，"
            "凭据请写进 auth（只写密钥名）"
        )


def _check_policy(manifest: PackageManifest, findings: _Findings) -> None:
    """对偏宽松的策略组合给出提醒。"""
    policy = manifest.spec.policy
    if policy.approval == "relaxed" and (policy.sandbox.shell or policy.sandbox.network):
        findings.warnings.append(
            "审批强度为 relaxed，同时开放了沙盒内的命令执行或出网；请确认这是有意为之"
        )
    if not manifest.spec.evals and not manifest.is_library:
        findings.warnings.append("没有评测用例：发布前无法自动验证这个包的效果")


def validate_package(
    root: Path, *, libraries: Mapping[str, Path] | None = None
) -> ValidationReport:
    """校验一个业务包目录。不抛异常，问题都在报告里。

    Args:
        root: 业务包根目录。
        libraries: 可选的本地技能库映射（``{包名: 目录}``）；给出时会一并校验 ``skills.uses``。
    """
    try:
        manifest = load_manifest(root / MANIFEST_FILENAME)
    except ManifestError as exc:
        problems = exc.problems or (str(exc),)
        return ValidationReport(manifest=None, errors=tuple(problems))
    findings = _Findings()
    skills_dir = manifest.spec.skills.dir
    local_skills: dict[str, SkillSummary] = {}
    if not is_safe_relative(skills_dir):
        findings.errors.append(f"skills.dir 必须是包内的相对路径：{skills_dir!r}")
    else:
        local_skills = _check_skills(manifest, scan_skills(root / skills_dir), findings)
    if manifest.spec.skills.uses and libraries is not None:
        _check_libraries(manifest, local_skills, libraries, findings)
    for connector in manifest.spec.connectors:
        if connector.transport == "http":
            _check_http_connector(root, connector, findings)
        elif connector.transport == "remote":
            _check_remote_connector(connector, findings)
    _check_policy(manifest, findings)
    return ValidationReport(
        manifest=manifest, errors=tuple(findings.errors), warnings=tuple(findings.warnings)
    )


__all__ = [
    "API_TOOL_PREFIX",
    "MCP_TOOL_PREFIX",
    "ValidationReport",
    "category_matches",
    "connector_tool_name",
    "is_safe_relative",
    "split_tool_name",
    "validate_package",
]

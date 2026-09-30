"""业务包目录的整体校验：清单本身，以及清单与包内容是否对得上。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from xuanpu_spec.errors import ManifestError
from xuanpu_spec.loader import load_manifest
from xuanpu_spec.manifest import MANIFEST_FILENAME, ConnectorSpec, PackageManifest
from xuanpu_spec.openapi import ApiDocument, OpenApiError, load_openapi
from xuanpu_spec.skills import scan_skills

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_spec.skills import SkillSummary

MCP_TOOL_PREFIX = "mcp__"
API_TOOL_PREFIX = "api__"
_TOOL_PREFIXES = (MCP_TOOL_PREFIX, API_TOOL_PREFIX)


def connector_tool_name(connector: str, tool: str) -> str:
    """模型看到的 MCP 连接器工具名。HTTP 连接器的工具名见 ``ConnectorSpec.tool_name``。"""
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


def _check_skills(
    manifest: PackageManifest, skills: list[SkillSummary], findings: _Findings
) -> None:
    """核对 skill 目录与清单声明的入口。"""
    by_id: dict[str, SkillSummary] = {}
    for skill in skills:
        if skill.problem is not None:
            findings.errors.append(f"skill 目录 {skill.directory!r}：{skill.problem}")
            continue
        if skill.skill_id in by_id:
            findings.errors.append(
                f"skill {skill.skill_id!r} 重复出现在目录 {by_id[skill.skill_id].directory!r} "
                f"与 {skill.directory!r}"
            )
            continue
        by_id[skill.skill_id] = skill
    if not skills:
        findings.errors.append(f"skill 目录 {manifest.spec.skills.dir!r} 不存在或为空")
    for entry in manifest.spec.skills.entries:
        declared_entry = by_id.get(entry)
        if declared_entry is None:
            findings.errors.append(f"入口 skill {entry!r} 在 skill 目录里找不到")
        elif not declared_entry.entry:
            findings.errors.append(f"入口 skill {entry!r} 的 SKILL.md 没有声明 entry: true")
    declared = set(manifest.spec.skills.entries)
    for skill in by_id.values():
        if skill.entry and skill.skill_id not in declared:
            findings.warnings.append(
                f"skill {skill.skill_id!r} 声明了 entry: true，但不在清单的 entries 里，"
                "不会作为会话入口开放"
            )
    _check_tool_references(manifest, list(by_id.values()), findings)


def _check_tool_references(
    manifest: PackageManifest, skills: list[SkillSummary], findings: _Findings
) -> None:
    """skill 引用的连接器工具必须对应清单里声明的连接器、正确的前缀与白名单。"""
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


def _check_policy(manifest: PackageManifest, findings: _Findings) -> None:
    """对偏宽松的策略组合给出提醒。"""
    policy = manifest.spec.policy
    if policy.approval == "relaxed" and (policy.sandbox.shell or policy.sandbox.network):
        findings.warnings.append(
            "审批强度为 relaxed，同时开放了沙盒内的命令执行或出网；请确认这是有意为之"
        )
    if not manifest.spec.evals:
        findings.warnings.append("没有评测用例：发布前无法自动验证这个包的效果")


def validate_package(root: Path) -> ValidationReport:
    """校验一个业务包目录。不抛异常，问题都在报告里。"""
    try:
        manifest = load_manifest(root / MANIFEST_FILENAME)
    except ManifestError as exc:
        problems = exc.problems or (str(exc),)
        return ValidationReport(manifest=None, errors=tuple(problems))
    findings = _Findings()
    skills_dir = manifest.spec.skills.dir
    if not is_safe_relative(skills_dir):
        findings.errors.append(f"skills.dir 必须是包内的相对路径：{skills_dir!r}")
    else:
        _check_skills(manifest, scan_skills(root / skills_dir), findings)
    for connector in manifest.spec.connectors:
        if connector.transport == "http":
            _check_http_connector(root, connector, findings)
    _check_policy(manifest, findings)
    return ValidationReport(
        manifest=manifest, errors=tuple(findings.errors), warnings=tuple(findings.warnings)
    )


__all__ = [
    "API_TOOL_PREFIX",
    "MCP_TOOL_PREFIX",
    "ValidationReport",
    "connector_tool_name",
    "is_safe_relative",
    "split_tool_name",
    "validate_package",
]

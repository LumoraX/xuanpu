"""L-Control 强类型确定性代码工具：修改 ``xuanpu.yaml`` 清单与策略（ADR 0025）。

铁律：绝不允许 LLM 用通用文件工具裸改 ``xuanpu.yaml``。
所有扩充/移除匠人入口、调整策略预算、配置连接器白名单、管理评测用例与版本号，
一律经本模块通过 ``spec.load_manifest`` → 强类型不变量校验 → ``spec.dump_manifest`` 规范化回写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from server.core.errors import InvalidRequestError
from server.domains.workspace.patch import make_unified_diff
from spec import MANIFEST_FILENAME, PackageManifest, dump_manifest, load_manifest

if TYPE_CHECKING:
    from pathlib import Path

_SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?$")
BumpKind = Literal["patch", "minor", "major"]


@dataclass(frozen=True)
class ControlMutationResult:
    """一次强类型清单修改的结果：新清单对象、语义 Unified Diff 与摘要。"""

    manifest: PackageManifest
    diff: str
    summary: str


def _manifest_path(draft_dir: Path) -> Path:
    """返回草稿目录下的 ``xuanpu.yaml`` 路径。"""
    return draft_dir / MANIFEST_FILENAME


def _apply_and_write(
    draft_dir: Path, mutator: Any, summary: str,
) -> ControlMutationResult:
    """加载现行清单 → 应用强类型字典变更 → 经 ``parse_manifest`` 复核 → 规范化回写并返回 Diff。"""
    path = _manifest_path(draft_dir)
    if not path.is_file():
        raise InvalidRequestError(f"草稿目录缺少 {MANIFEST_FILENAME}")
    before_text = path.read_text(encoding="utf-8")
    current = load_manifest(path)
    raw = current.model_dump(mode="json", by_alias=True, exclude_none=True)
    mutator(raw)
    updated = PackageManifest.model_validate(raw)
    after_text = dump_manifest(updated)
    path.write_text(after_text, encoding="utf-8")
    diff = make_unified_diff(before_text, after_text, filename=MANIFEST_FILENAME)
    return ControlMutationResult(manifest=updated, diff=diff, summary=summary)


def forge_add_entry(
    draft_dir: Path,
    *,
    skill_id: str,
    display_name: str | None = None,
    description: str | None = None,
) -> ControlMutationResult:
    """强类型扩充匠人入口，并按需初始化标准 ``skills/<skill_id>/SKILL.md`` 骨架。"""
    if not _SKILL_ID_RE.fullmatch(skill_id):
        raise InvalidRequestError(f"无效技能入口标识：{skill_id!r}")

    def _mutate(raw: dict[str, Any]) -> None:
        skills = raw.setdefault("spec", {}).setdefault("skills", {})
        entries: list[str] = list(skills.get("entries") or [])
        if skill_id in entries:
            raise InvalidRequestError(f"入口 {skill_id} 已存在")
        entries.append(skill_id)
        skills["entries"] = entries

    res = _apply_and_write(draft_dir, _mutate, f"新增匠人入口 {skill_id}")
    skills_dir = draft_dir / res.manifest.spec.skills.dir / skill_id
    skill_file = skills_dir / "SKILL.md"
    if not skill_file.exists():
        skills_dir.mkdir(parents=True, exist_ok=True)
        title = display_name or skill_id
        desc = description or f"{title} 技能入口"
        skill_file.write_text(
            f"---\nname: {skill_id}\ndisplay_name: {title}\ndescription: {desc}\n"
            f"version: 1.0.0\ntype: composite\nentry: true\ntool_names: [file_read]\n---\n\n# {title}\n\n{desc}\n",
            encoding="utf-8",
        )
    return res


def forge_remove_entry(draft_dir: Path, *, skill_id: str) -> ControlMutationResult:
    """强类型移除/禁用匠人入口；若为最后一个入口且无共享导出则拒绝，防止破坏规范。"""

    def _mutate(raw: dict[str, Any]) -> None:
        skills = raw.setdefault("spec", {}).setdefault("skills", {})
        entries: list[str] = list(skills.get("entries") or [])
        if skill_id not in entries:
            raise InvalidRequestError(f"入口 {skill_id} 不存在")
        entries.remove(skill_id)
        exports = list(skills.get("exports") or [])
        if not entries and not exports:
            raise InvalidRequestError("不能移除最后一个入口（包至少须保留一个入口或共享导出）")
        skills["entries"] = entries
        # 同步清理指向已移除入口的评测用例，避免残留悬空引用
        evals = raw.get("spec", {}).get("evals")
        if isinstance(evals, list):
            raw["spec"]["evals"] = [e for e in evals if e.get("entry") != skill_id]

    return _apply_and_write(draft_dir, _mutate, f"移除匠人入口 {skill_id}")


def forge_set_policy(
    draft_dir: Path,
    *,
    approval: Literal["strict", "standard", "relaxed"] | None = None,
    max_session_tokens: int | None = None,
    max_iterations: int | None = None,
    sandbox_network: bool | None = None,
    sandbox_shell: bool | None = None,
    model_tier: Literal["fast", "balanced", "deep"] | None = None,
) -> ControlMutationResult:
    """强类型修改策略、预算、沙盒开关与模型档位。"""

    def _mutate(raw: dict[str, Any]) -> None:
        spec = raw.setdefault("spec", {})
        policy = spec.setdefault("policy", {})
        if approval is not None:
            policy["approval"] = approval
        if max_session_tokens is not None or max_iterations is not None:
            budget = policy.setdefault("budget", {})
            if max_session_tokens is not None:
                budget["maxSessionTokens"] = max_session_tokens
            if max_iterations is not None:
                budget["maxIterations"] = max_iterations
        if sandbox_network is not None or sandbox_shell is not None:
            sandbox = policy.setdefault("sandbox", {})
            if sandbox_network is not None:
                sandbox["network"] = sandbox_network
            if sandbox_shell is not None:
                sandbox["shell"] = sandbox_shell
        if model_tier is not None:
            spec.setdefault("model", {})["tier"] = model_tier

    return _apply_and_write(draft_dir, _mutate, "更新匠人策略与预算配置")


def forge_set_connector_tools(
    draft_dir: Path,
    *,
    connector_name: str,
    tools: list[dict[str, Any]],
) -> ControlMutationResult:
    """强类型配置连接器工具白名单：严禁空通配或 ``*``，确保最小权限。"""
    if not tools:
        raise InvalidRequestError("连接器工具白名单不能为空（禁止全量通配暴露）")
    for item in tools:
        name = str(item.get("name") or "").strip()
        if not name or "*" in name:
            raise InvalidRequestError(f"连接器工具名禁止通配符或为空：{name!r}")

    def _mutate(raw: dict[str, Any]) -> None:
        connectors: list[dict[str, Any]] = list(raw.get("spec", {}).get("connectors") or [])
        target = next((c for c in connectors if c.get("name") == connector_name), None)
        if target is None:
            raise InvalidRequestError(f"连接器 {connector_name} 不存在")
        target["tools"] = tools
        raw["spec"]["connectors"] = connectors

    return _apply_and_write(draft_dir, _mutate, f"更新连接器 {connector_name} 白名单")


def forge_upsert_eval(
    draft_dir: Path,
    *,
    name: str,
    entry: str,
    input_text: str,
    contains: tuple[str, ...] = (),
    not_contains: tuple[str, ...] = (),
    max_tokens: int | None = None,
) -> ControlMutationResult:
    """强类型新增或更新评测用例。"""

    def _mutate(raw: dict[str, Any]) -> None:
        spec = raw.setdefault("spec", {})
        entries = list(spec.get("skills", {}).get("entries") or [])
        if entry not in entries:
            raise InvalidRequestError(f"评测目标入口 {entry} 不在 entries 中")
        expect: dict[str, Any] = {}
        if contains:
            expect["contains"] = list(contains)
        if not_contains:
            expect["notContains"] = list(not_contains)
        if max_tokens is not None:
            expect["maxTokens"] = max_tokens
        case = {"name": name, "entry": entry, "input": input_text, "expect": expect}
        evals: list[dict[str, Any]] = [
            e for e in (spec.get("evals") or []) if e.get("name") != name
        ]
        evals.append(case)
        spec["evals"] = evals

    return _apply_and_write(draft_dir, _mutate, f"登记评测用例 {name}")


def forge_bump_version(draft_dir: Path, *, bump: BumpKind = "patch") -> ControlMutationResult:
    """强类型递增业务包语义化版本，确保每次发布不可变且单调递增。"""

    def _mutate(raw: dict[str, Any]) -> None:
        meta = raw.setdefault("metadata", {})
        old = str(meta.get("version") or "")
        match = _SEMVER_RE.fullmatch(old)
        if match is None:
            raise InvalidRequestError(f"当前版本号格式不合法：{old!r}")
        major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if bump == "major":
            meta["version"] = f"{major + 1}.0.0"
        elif bump == "minor":
            meta["version"] = f"{major}.{minor + 1}.0"
        else:
            meta["version"] = f"{major}.{minor}.{patch + 1}"

    return _apply_and_write(draft_dir, _mutate, f"语义化递增版本（{bump}）")

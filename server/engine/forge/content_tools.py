"""L-Content 受控内容编辑工具与 ``taifeng.HookRegistry`` 5 级安全护栏（ADR 0025）。

- 复用 ``taifeng.make_apply_patch_tool``（结构化 edit/create/delete + 全量 dry-run 原子写）
  与 ``taifeng.HookRegistry``；
- ``PreToolUseHook`` 硬拒绝任何试图裸改 ``xuanpu.yaml`` 或越出白名单目录的补丁；
- ``PostToolUseHook`` 在写入后自动触发 Python 语法检查（``compile``）与 Skill frontmatter 扫描，
  并产出结构化 Unified Diff。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import taifeng

from server.domains.workspace.patch import make_unified_diff
from spec import MANIFEST_FILENAME
from spec.skills import scan_skills

_ALLOWED_PREFIXES = ("skills/", "tests/")
_FORBIDDEN_NAMES = frozenset({MANIFEST_FILENAME, "delivery.json"})


def is_content_path_allowed(relpath: str) -> bool:
    """校验相对路径是否严格位于允许内容编辑的子目录内，显式拒绝清单与路径穿越。"""
    normalized = relpath.strip().lstrip("./").replace("\\", "/")
    if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
        return False
    if Path(normalized).name in _FORBIDDEN_NAMES or normalized in _FORBIDDEN_NAMES:
        return False
    return normalized.startswith(_ALLOWED_PREFIXES)


@dataclass(frozen=True)
class ContentPatchOutcome:
    """受控内容补丁执行结果：是否允许、输出消息、改动文件 Diff 与自动校验告警。"""

    ok: bool
    message: str
    diffs: dict[str, str]
    post_checks: tuple[str, ...]


class ForgeContentEditor:
    """基于 ``taifeng`` ``HookRegistry`` + ``make_apply_patch_tool`` 的受控内容编辑器。"""

    def __init__(self, draft_dir: Path) -> None:
        """绑定单个造匠草稿目录，并装配 Pre/Post 生命周期 Hook。"""
        self._draft_dir = draft_dir.resolve()
        self._fs = taifeng.LocalWorkspaceFS(self._draft_dir)
        self._patch_tool = taifeng.make_apply_patch_tool(workspace=self._fs)
        self._hooks = taifeng.HookRegistry()
        self._hooks.register("pre_tool_use", self._pre_guard)
        self._hooks.register("post_tool_use", self._post_check)
        self._runner = taifeng.HookRunner(self._hooks)

    async def _pre_guard(
        self, hook: taifeng.PreToolUseHook, _ctx: taifeng.HookContext,
    ) -> taifeng.HookDecision:
        """L1 硬安全护栏：阻止任何裸改 ``xuanpu.yaml`` 或越界路径的补丁。"""
        patches = hook.arguments.get("patches")
        if not isinstance(patches, list) or not patches:
            return taifeng.HookDecision.deny("patches 必须是非空列表")
        for patch in patches:
            path = str(patch.get("path") or "") if isinstance(patch, dict) else ""
            if Path(path.strip()).name in _FORBIDDEN_NAMES:
                return taifeng.HookDecision.deny(
                    f"禁止通过文件工具直接编辑 {MANIFEST_FILENAME}，请使用 L-Control 强类型代码工具"
                    "（forge_add_entry / forge_set_policy / forge_bump_version 等）"
                )
            if not is_content_path_allowed(path):
                return taifeng.HookDecision.deny(
                    f"路径 {path!r} 不在允许的内容白名单（skills/ 或 tests/）内"
                )
        return taifeng.HookDecision.ok()

    async def _post_check(
        self, hook: taifeng.PostToolUseHook, ctx: taifeng.HookContext,
    ) -> taifeng.HookDecision:
        """写入后自动运行确定性静态检查（Python 语法 + SKILL.md 扫描）。"""
        if hook.is_error:
            return taifeng.HookDecision.ok()
        warnings: list[str] = ctx.extras.setdefault("warnings", [])
        patches = hook.arguments.get("patches") or []
        touched_skill = False
        for patch in patches:
            if not isinstance(patch, dict) or patch.get("delete"):
                continue
            rel = str(patch.get("path") or "")
            target = (self._draft_dir / rel).resolve()
            if rel.endswith(".py") and target.is_file():
                try:
                    source = await asyncio.to_thread(target.read_text, encoding="utf-8")
                    compile(source, rel, "exec")
                except SyntaxError as exc:
                    warnings.append(f"{rel} 存在 Python 语法错误（第 {exc.lineno} 行）：{exc.msg}")
            if target.name == "SKILL.md":
                touched_skill = True
        if touched_skill and (self._draft_dir / "skills").is_dir():
            scanned = await asyncio.to_thread(scan_skills, self._draft_dir / "skills")
            for item in scanned:
                if item.problem is not None:
                    warnings.append(f"技能 {item.skill_id} 存在规范问题：{item.problem}")
        return taifeng.HookDecision.ok()

    async def apply_patches(self, patches: list[dict[str, Any]]) -> ContentPatchOutcome:
        """按 PreToolUseHook → taifeng apply_patch 原子干跑落盘 → PostToolUseHook 顺序执行。"""
        warnings_box: list[str] = []
        ctx = taifeng.HookContext(
            thread_id="forge",
            submission_id="forge_patch",
            entry_skill_id="artisan-forge",
            extras={"warnings": warnings_box},
        )
        args = {"patches": patches}
        pre = await self._runner.run(
            "pre_tool_use",
            taifeng.PreToolUseHook(
                tool_name="apply_patch", arguments=args, parallel_safe=False, call_id="forge_1"
            ),
            ctx,
        )
        if not pre.allow:
            return ContentPatchOutcome(
                ok=False, message=pre.reason or "补丁被安全护栏拒绝", diffs={}, post_checks=()
            )

        # 记录改动前内容用于生成 Unified Diff
        before_map: dict[str, str] = {}
        for patch in patches:
            rel = str(patch.get("path") or "")
            file_path = (self._draft_dir / rel).resolve()
            if file_path.is_file():
                before_map[rel] = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
            else:
                before_map[rel] = ""

        result = await self._patch_tool.handler(
            args,
            taifeng.ToolContext(
                call_id="forge_1", cancel=taifeng.CancellationToken(), thread_id="forge"
            ),
        )
        await self._runner.run(
            "post_tool_use",
            taifeng.PostToolUseHook(
                tool_name="apply_patch", arguments=args, output=result.output,
                is_error=result.is_error, duration_ms=0, call_id="forge_1",
            ),
            ctx,
        )
        if result.is_error:
            return ContentPatchOutcome(ok=False, message=result.output, diffs={}, post_checks=())

        diffs: dict[str, str] = {}
        for rel, before_text in before_map.items():
            file_path = (self._draft_dir / rel).resolve()
            after_text = (
                await asyncio.to_thread(file_path.read_text, encoding="utf-8")
                if file_path.is_file()
                else ""
            )
            diffs[rel] = make_unified_diff(before_text, after_text, filename=rel)

        return ContentPatchOutcome(
            ok=True, message=result.output, diffs=diffs, post_checks=tuple(warnings_box)
        )

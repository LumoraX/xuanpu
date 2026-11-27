"""个人文件与内容层的精准局部块编辑引擎（ADR 0025）。

- 严格唯一锚定原则：每条替换的 ``target`` 必须在当前正文中恰好出现一次；
  未命中或多处歧义直接拒绝，绝不做近似替换或静默覆盖。
- 全量试跑（dry-run）：全部编辑按顺序校验通过后才返回新正文与标准 Unified Diff。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from server.core.errors import InvalidRequestError

if TYPE_CHECKING:
    from collections.abc import Sequence

MAX_EDITS_PER_PATCH = 20


class TextPatchEdit(BaseModel):
    """单条精确局部替换操作。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    target: str = Field(min_length=1, max_length=50_000)
    replacement: str = Field(max_length=50_000)


@dataclass(frozen=True)
class PatchResult:
    """局部编辑结果：新正文、标准 Unified Diff 与生效条数。"""

    new_text: str
    diff: str
    applied_count: int


def make_unified_diff(original: str, updated: str, *, filename: str) -> str:
    """生成标准 Unified Diff 字符串；对超长文档自动裁切首尾未变更公共行，避免 O(N^2) 性能退化。"""
    orig_lines = original.splitlines()
    upd_lines = updated.splitlines()
    # 先跳过首尾完全相同的公共前后缀，仅保留变更窗上下各 3 行上下文做精确 Diff
    prefix = 0
    min_len = min(len(orig_lines), len(upd_lines))
    while prefix < min_len and orig_lines[prefix] == upd_lines[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < (min_len - prefix)
        and orig_lines[len(orig_lines) - 1 - suffix] == upd_lines[len(upd_lines) - 1 - suffix]
    ):
        suffix += 1
    if prefix == len(orig_lines) == len(upd_lines):
        return ""

    ctx = 3
    start = max(0, prefix - ctx)
    orig_end = len(orig_lines) - max(0, suffix - ctx)
    upd_end = len(upd_lines) - max(0, suffix - ctx)

    if start == 0 and orig_end == len(orig_lines) and upd_end == len(upd_lines):
        diff_lines = difflib.unified_diff(
            [ln + "\n" for ln in orig_lines],
            [ln + "\n" for ln in upd_lines],
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm="",
        )
        return "\n".join(line.rstrip("\n") for line in diff_lines)

    slice_orig = [ln + "\n" for ln in orig_lines[start:orig_end]]
    slice_upd = [ln + "\n" for ln in upd_lines[start:upd_end]]
    raw_diff = list(
        difflib.unified_diff(
            slice_orig,
            slice_upd,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm="",
        )
    )
    # 将局部切片的 hunk 起始行号平移回真实全文行号
    adjusted: list[str] = []
    for line in raw_diff:
        if line.startswith("@@ "):
            parts = line.split(" ")
            if len(parts) >= 4 and parts[1].startswith("-") and parts[2].startswith("+"):
                a_info = parts[1][1:].split(",")
                b_info = parts[2][1:].split(",")
                a_start = int(a_info[0]) + start
                b_start = int(b_info[0]) + start
                a_span = f",{a_info[1]}" if len(a_info) > 1 else ""
                b_span = f",{b_info[1]}" if len(b_info) > 1 else ""
                adjusted.append(f"@@ -{a_start}{a_span} +{b_start}{b_span} @@")
                continue
        adjusted.append(line.rstrip("\n"))
    return "\n".join(adjusted)


def replace_line_range(
    original: str,
    *,
    start_line: int,
    end_line: int,
    replacement: str,
    filename: str = "document.md",
) -> PatchResult:
    """按 1-based 精确行号区间替换超长文档片段并返回快速 Unified Diff。"""
    lines = original.splitlines()
    total = len(lines)
    if start_line < 1 or end_line < start_line or start_line > max(1, total):
        raise InvalidRequestError(f"行号区间无效：{start_line}..{end_line}（文档共 {total} 行）")
    clamped_end = min(end_line, total)
    rep_lines = replacement.splitlines() if replacement else []
    new_lines = lines[: start_line - 1] + rep_lines + lines[clamped_end:]
    trailing = "\n" if original.endswith("\n") else ""
    new_text = "\n".join(new_lines) + trailing
    diff = make_unified_diff(original, new_text, filename=filename)
    return PatchResult(new_text=new_text, diff=diff, applied_count=1)


def apply_exact_edits(
    original: str,
    edits: Sequence[TextPatchEdit],
    *,
    filename: str = "document.md",
) -> PatchResult:
    """按顺序执行全量干跑校验，保证每条 target 精确唯一命中后再生成 Unified Diff。"""
    if not edits:
        raise InvalidRequestError("edits 至少包含一条替换项")
    if len(edits) > MAX_EDITS_PER_PATCH:
        raise InvalidRequestError(f"单次最多提交 {MAX_EDITS_PER_PATCH} 条局部编辑")

    current = original
    for idx, edit in enumerate(edits, start=1):
        count = current.count(edit.target)
        if count == 0:
            raise InvalidRequestError(
                f"第 {idx} 条编辑未找到精确匹配的目标片段，请先读取最新正文再修改"
            )
        if count > 1:
            raise InvalidRequestError(
                f"第 {idx} 条编辑的目标片段出现 {count} 处歧义，请提供更长上下文以唯一锚定"
            )
        current = current.replace(edit.target, edit.replacement, 1)

    diff = make_unified_diff(original, current, filename=filename)
    return PatchResult(new_text=current, diff=diff, applied_count=len(edits))

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
    """生成标准 Unified Diff 字符串，供前端 Diff 面板与审核记录展示。"""
    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        lineterm="",
    )
    return "\n".join(line.rstrip("\n") for line in diff_lines)


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

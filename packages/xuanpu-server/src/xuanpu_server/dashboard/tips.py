"""提示：确定性生成、最多 3 条、不调模型。

依据只有：本人此刻可用的匠人、概览各块的数量与是否读取失败、是否首次使用。系统状态提示
（概览部分读不出）占同一位置并排在最前；管家不可用、额度用完等由页面在请求失败时补充。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

TIPS_MAX = 3
TipKind = Literal["status", "try"]


@dataclass(frozen=True)
class Tip:
    """一条提示。``prompt`` 是点一下后填进输入框的话；状态提示没有。"""

    kind: TipKind
    text: str
    prompt: str | None = None

    def view(self) -> dict[str, Any]:
        """接口视图。"""
        return {"kind": self.kind, "text": self.text, "prompt": self.prompt}


def _count(blocks: list[dict[str, Any]], key: str) -> int:
    """某块的数量；块不存在或失败按 0。"""
    for block in blocks:
        if block.get("key") == key and block.get("status") == "ok":
            return int(block.get("count") or 0)
    return 0


def build_tips(
    blocks: list[dict[str, Any]], *, artisans: list[str], first_use: bool,
) -> list[Tip]:
    """按固定优先级生成：状态 → 首次使用 → 等我处理 → 今天 → 可用匠人 → 通用。"""
    tips: list[Tip] = []
    if any(block.get("status") != "ok" for block in blocks):
        tips.append(Tip("status", "部分概览暂时读不出；可以直接说需求，或在「更多」里查看。"))
    if first_use:
        tips.append(Tip("try", "直接说出你要办的事", "明天上午9点提醒我准备周报"))
    waiting = _count(blocks, "waiting")
    if waiting:
        tips.append(Tip("try", f"有 {waiting} 件事在等你处理", "看看等我处理的事"))
    if _count(blocks, "today"):
        tips.append(Tip("try", "看看今天的安排", "看看我今天的日程和待办"))
    for name in artisans[:2]:
        tips.append(Tip("try", f"可以请「{name}」帮忙", f"请{name}帮我"))
    tips.append(Tip("try", "记一件待办", "帮我记一件待办："))
    return tips[:TIPS_MAX]

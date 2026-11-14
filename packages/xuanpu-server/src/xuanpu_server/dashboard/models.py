"""概览的数据形状：每块一个数量加至多 5 条摘要；读取失败的块单独标记。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SUMMARY_MAX = 5
TITLE_MAX = 80

BlockKey = Literal["today", "waiting", "in_progress", "unread"]
BLOCK_ORDER: tuple[BlockKey, ...] = ("today", "waiting", "in_progress", "unread")


def short(text: str) -> str:
    """摘要标题去掉换行并截到上限。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= TITLE_MAX else flat[: TITLE_MAX - 1] + "…"


@dataclass(frozen=True)
class Summary:
    """一条摘要：类型、引用、标题与可选时间（UTC 秒）。

    ``ref`` 是点击后定位用的标识（提醒、日程、待办、待处理项、trace 等），不含正文。
    """

    kind: str
    ref: str
    title: str
    at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        """接口视图。"""
        return {"kind": self.kind, "ref": self.ref, "title": self.title, "at": self.at, **self.extra}


@dataclass(frozen=True)
class Block:
    """一块概览。``count`` 在授权过滤之后对全部记录计算，不从摘要推算。"""

    key: BlockKey
    count: int
    items: tuple[Summary, ...]

    def view(self) -> dict[str, Any]:
        """接口视图。"""
        return {"key": self.key, "status": "ok", "count": self.count,
                "items": [item.view() for item in self.items]}


def failed_view(key: BlockKey, code: str) -> dict[str, Any]:
    """读取失败的块：只给错误码，不给数量，不拖垮其他块。"""
    return {"key": key, "status": "error", "error": code, "count": None, "items": []}

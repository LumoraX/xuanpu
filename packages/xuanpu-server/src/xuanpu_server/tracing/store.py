"""span 存储契约：memory 与 PostgreSQL 实现同一套行为，测试共用。"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.tracing.models import DEFAULT_SOURCE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from xuanpu_server.tracing.models import (
        SpanFact,
        TraceOrigin,
        TraceOwner,
        TraceRecord,
        TraceView,
    )

PAGE_DEFAULT = 50
PAGE_MAX = 100


@dataclass(frozen=True)
class TracePage:
    """一页 trace。``total`` 在归属与状态过滤之后计算，与当前页无关。"""

    items: tuple[TraceRecord, ...]
    next_cursor: str | None
    total: int


class SpanStore(Protocol):
    """按租户与员工隔离的 trace / span 存储。

    归属不符与不存在一律表现为 ``NotFoundError``；接入层据此返回 404。
    """

    async def open_trace(
        self, owner: TraceOwner, *, trace_id: str, origin: TraceOrigin, title: str, at: datetime,
    ) -> TraceRecord:
        """建一条 trace；同一归属重复调用返回原记录，被他人占用的标识报冲突。"""
        ...

    async def apply(
        self, owner: TraceOwner, trace_id: str, facts: Sequence[SpanFact], *,
        source: str = DEFAULT_SOURCE,
        max_spans: int | None = None, max_attempts: int | None = None,
    ) -> TraceRecord:
        """按写入方 ``source`` 的序号应用事实：该写入方已应用的序号跳过，跳号与被拒事实把
        trace 标为不完整；不同写入方的序号互不影响。

        ``max_spans`` / ``max_attempts`` 覆盖构造时的上限（采集器按配置传入）。
        """
        ...

    async def mark_incomplete(self, owner: TraceOwner, trace_id: str) -> TraceRecord:
        """采集失败等情况下显式标记记录不完整。"""
        ...

    async def get(self, owner: TraceOwner, trace_id: str) -> TraceView:
        """读取完整的一条 trace。"""
        ...

    async def list(
        self, owner: TraceOwner, *, active: bool | None = None,
        cursor: str | None = None, limit: int = PAGE_DEFAULT,
    ) -> TracePage:
        """按创建时间倒序分页列出本人的 trace。"""
        ...

    async def purge(self, tenant_id: str, *, before: datetime, batch: int = 500) -> int:
        """删除某租户早于 ``before`` 创建的 trace 及其 span，返回删除条数；不动审计。"""
        ...


def check_limit(limit: int) -> int:
    """分页大小必须在 1..100。"""
    if not 1 <= limit <= PAGE_MAX:
        raise InvalidRequestError(f"limit 必须在 1 到 {PAGE_MAX} 之间")
    return limit


def encode_cursor(created_at: datetime, trace_id: str) -> str:
    """游标编码排序键：创建时间加标识，保证稳定。"""
    raw = f"{created_at.isoformat()}|{trace_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解析游标；格式不对报请求错误，不当作从头开始。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, trace_id = base64.urlsafe_b64decode(padded).decode().split("|", 1)
        return datetime.fromisoformat(stamp), trace_id
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidRequestError("cursor 无效") from exc

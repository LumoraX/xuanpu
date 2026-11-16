"""由持久 span 计算执行树深度与瀑布行；缺父、成环都有界处理，不递归挂死。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import timedelta

    from xuanpu_server.tracing.models import SpanKind, SpanRecord, SpanState, TraceView


@dataclass(frozen=True)
class WaterfallRow:
    """一行瀑布。运行中的行 ``duration_ms`` 为 None，不编造时长。"""

    span_id: str
    parent_span_id: str | None
    kind: SpanKind
    name: str
    state: SpanState
    depth: int
    offset_ms: int
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    usage_complete: bool
    orphan: bool


def waterfall(view: TraceView, *, max_depth: int = 32) -> tuple[WaterfallRow, ...]:
    """按开始时间排序输出；找不到父级或成环的 span 记为孤儿并从深度 0 开始。"""
    if not view.spans:
        return ()
    by_id = {span.span_id: span for span in view.spans}
    origin = min(span.started_at for span in view.spans)
    rows = []
    for span in sorted(view.spans, key=lambda s: (s.started_at, s.span_id)):
        depth, orphan = _depth(span, by_id, max_depth)
        duration = None
        if span.ended_at is not None:
            duration = max(0, _ms(span.ended_at - span.started_at))
        rows.append(WaterfallRow(
            span_id=span.span_id, parent_span_id=span.parent_span_id, kind=span.kind,
            name=span.name, state=span.state, depth=depth,
            offset_ms=max(0, _ms(span.started_at - origin)), duration_ms=duration,
            input_tokens=span.input_tokens, output_tokens=span.output_tokens,
            usage_complete=span.usage_complete, orphan=orphan,
        ))
    return tuple(rows)


def _depth(span: SpanRecord, by_id: dict[str, SpanRecord], max_depth: int) -> tuple[int, bool]:
    """沿父链向上数层级；遇到缺父、成环或超深即停并标孤儿。"""
    depth, seen, current = 0, {span.span_id}, span
    while current.parent_span_id is not None:
        parent = by_id.get(current.parent_span_id)
        if parent is None or parent.span_id in seen or depth >= max_depth:
            return 0, True
        seen.add(parent.span_id)
        depth += 1
        current = parent
    return depth, False


def _ms(delta: timedelta) -> int:
    """timedelta 转整数毫秒。"""
    return int(delta.total_seconds() * 1000)

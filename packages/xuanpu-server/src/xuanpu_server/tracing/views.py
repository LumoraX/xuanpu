"""执行追踪的对外视图：只含元数据，不回显租户与员工标识、不含正文。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xuanpu_server.tracing.models import SpanRecord, TraceRecord, TraceView


def trace_view(record: TraceRecord) -> dict[str, Any]:
    """trace 头：归属只给本人，不重复回显租户与员工标识。"""
    return {
        "trace_id": record.trace_id, "origin": record.origin, "title": record.title,
        "created_at": record.created_at.isoformat(), "updated_at": record.updated_at.isoformat(),
        "incomplete": record.incomplete, "truncated": record.truncated,
    }


def span_view(span: SpanRecord) -> dict[str, Any]:
    """一个 span：用量是各尝试自身用量之和，未知为 null。"""
    return {
        "span_id": span.span_id, "parent_span_id": span.parent_span_id, "kind": span.kind,
        "name": span.name, "ref": span.ref, "state": span.state,
        "started_at": span.started_at.isoformat(),
        "ended_at": None if span.ended_at is None else span.ended_at.isoformat(),
        "attempts": len(span.attempts), "input_tokens": span.input_tokens,
        "output_tokens": span.output_tokens, "usage_complete": span.usage_complete,
        "end_reason": span.end_reason, "incomplete": span.incomplete,
    }


def tree_view(view: TraceView) -> dict[str, Any]:
    """完整执行树。"""
    return {**trace_view(view.trace), "active": view.active,
            "spans": [span_view(span) for span in view.spans]}

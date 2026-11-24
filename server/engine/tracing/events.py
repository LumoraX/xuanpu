"""内核事件 → span 事实草稿：纯函数，只读事件的身份与有限标量字段，不读正文。

每个执行（内核 ``ExecutionIdentity.execution_id``）是一个 span，``ex.<execution_id>``；
每次工具调用是一个 span，``tl.<call_id>``，挂在发起它的执行下。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from server.engine.runtime.operation_status import operation_outcome
from server.engine.tracing.models import AttemptUsage

if TYPE_CHECKING:
    import taifeng

    from server.engine.tracing.models import SpanKind, SpanState

_SAFE = re.compile(r"^[A-Za-z0-9_.:-]{1,150}$")
# 这些工具在等子执行：调用期间执行本身显示为 waiting_children
WAITING_TOOLS = frozenset({"call_skill", "await_skills", "join_skill", "wait_peer", "wait_any"})
_PHASES: dict[str, SpanState] = {
    "model": "running", "running": "running", "failed": "failed", "cancelled": "cancelled",
}
_ENDINGS: dict[str, SpanState] = {
    "completed": "completed", "cancelled": "cancelled", "failed": "failed",
    "suspended": "waiting_input", "outcome_unknown": "waiting_input",
}


@dataclass(frozen=True)
class Draft:
    """还没编序号的事实；字段含义同 ``SpanFact``。"""

    span_id: str
    at: datetime
    kind: SpanKind | None = None
    name: str | None = None
    parent_span_id: str | None = None
    ref: str | None = None
    attempt_id: str | None = None
    state: SpanState | None = None
    usage: AttemptUsage | None = None
    end_reason: str | None = None
    say: str | None = None

    def fields(self) -> dict[str, Any]:
        """转成 ``SpanFact`` 的关键字参数（不含序号）。"""
        return {key: value for key, value in self.__dict__.items() if value is not None}


def span_key(prefix: str, raw: str) -> str:
    """把内核或模型给出的标识变成合法 span 标识；含非常规字符时取摘要，不截断冲突。"""
    if _SAFE.fullmatch(raw):
        return f"{prefix}.{raw}"
    return f"{prefix}.h{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def execution_span(execution_id: str) -> str:
    """执行 span 的标识。"""
    return span_key("ex", execution_id)


def event_time(event: taifeng.EventMsg) -> datetime:
    """事件时间；没有时区的按 UTC。"""
    stamp = event.timestamp
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def drafts(event: taifeng.EventMsg, *, root_kind: SpanKind, ref: str | None) -> list[Draft]:
    """一条事件 → 零到两条草稿。没有执行身份的事件不产生事实。"""
    identity = event.execution
    if identity is None:
        return []
    kind, data, at = event.msg.kind, event.msg.data, event_time(event)
    own = execution_span(identity.execution_id)
    attempt = identity.attempt_id
    if kind == "turn_started":
        parent = identity.parent_execution_id
        return [Draft(
            own, at, kind=root_kind if parent is None else "artisan", name=identity.skill_id,
            parent_span_id=None if parent is None else execution_span(parent),
            ref=ref if parent is None else None, attempt_id=attempt, state="running",
        )]
    if kind == "execution_updated":
        return [Draft(own, at, attempt_id=attempt, state=_PHASES.get(str(data.get("phase"))),
                      usage=_usage(attempt, data))]
    if kind in ("tool_call_started", "tool_call_completed"):
        return _tool(kind, data, own, at)
    if kind in ("turn_suspended", "turn_completed", "turn_failed"):
        outcome = operation_outcome(kind, data)
        state = _ENDINGS.get(outcome, "failed")
        reason = outcome if state in ("failed", "cancelled") else None
        return [Draft(own, at, attempt_id=attempt, state=state, end_reason=reason,
                      usage=_usage(attempt, data) if kind != "turn_suspended" else None)]
    return []


def _tool(kind: str, data: dict[str, Any], owner: str, at: datetime) -> list[Draft]:
    """工具调用 span；等子执行的工具同时改变所属执行的状态。"""
    call = data.get("call_id")
    name = data.get("name")
    if not isinstance(call, str) or not call or not isinstance(name, str) or not name:
        return []
    span = span_key("tl", call)
    waiting = name in WAITING_TOOLS
    if kind == "tool_call_started":
        found = [Draft(span, at, kind="tool", name=name, parent_span_id=owner, state="running")]
        return found + ([Draft(owner, at, state="waiting_children")] if waiting else [])
    failed = data.get("is_error") is True
    done = Draft(span, at, state="failed" if failed else "completed",
                 end_reason="tool_error" if failed else None)
    return [done] + ([Draft(owner, at, state="running")] if waiting else [])


def _usage(attempt: str, data: dict[str, Any]) -> AttemptUsage | None:
    """取本尝试自身的用量绝对值；字段缺失或不是非负整数时记为未知。"""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens")]
    valid = [value if isinstance(value, int) and not isinstance(value, bool) and value >= 0
             else None for value in counts]
    complete = data.get("usage_complete") is True and None not in valid
    return AttemptUsage(attempt_id=attempt, input_tokens=valid[0], output_tokens=valid[1],
                        usage_complete=complete)

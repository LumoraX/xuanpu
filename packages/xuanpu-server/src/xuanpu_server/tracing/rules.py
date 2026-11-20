"""span 状态转换的纯函数：同一事实重复到达不改变结果，终态不被同一尝试的迟到事实复活。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.tracing.models import TERMINAL_STATES, AttemptUsage, SpanRecord

if TYPE_CHECKING:
    from xuanpu_server.tracing.models import SpanFact


class FactRejectedError(ValueError):
    """事实缺少建立新 span 所需的字段，或与已有 span 冲突。"""


def new_span(fact: SpanFact) -> SpanRecord:
    """第一次见到某 span：必须给出类型与显示名。"""
    if fact.kind is None or fact.name is None:
        raise FactRejectedError("新 span 必须带 kind 与 name")
    record = SpanRecord(
        span_id=fact.span_id, kind=fact.kind, name=fact.name,
        parent_span_id=fact.parent_span_id, ref=fact.ref, started_at=fact.at, say=fact.say,
    )
    return apply_fact(record, fact, max_attempts=None)


def apply_fact(span: SpanRecord, fact: SpanFact, *, max_attempts: int | None) -> SpanRecord:
    """把一条事实应用到已有 span，返回新快照。

    规则：
    - 类型、父级、引用一经确定不再改变；冲突视为不完整而不是覆盖。
    - 同一尝试到达终态后，它的迟到状态事实不复活 span；新尝试可以重新运行。
    - 用量按尝试保存绝对值，重复快照覆盖而非累加；尝试数超上限时丢弃并标不完整。
    - 旁白首值为准。
    """
    incomplete = span.incomplete or _conflicts(span, fact)
    attempt = fact.attempt_id or span.current_attempt_id
    new_attempt = fact.attempt_id is not None and fact.attempt_id != span.current_attempt_id
    state, ended_at, end_reason = span.state, span.ended_at, span.end_reason
    if fact.state is not None and (new_attempt or span.state not in TERMINAL_STATES):
        state = fact.state
        ended_at = fact.at if state in TERMINAL_STATES else None
        end_reason = fact.end_reason if state in TERMINAL_STATES else None
    attempts, dropped = _merge_usage(span.attempts, fact.usage, max_attempts)
    return span.model_copy(update={
        "name": span.name if fact.name is None else fact.name,
        "parent_span_id": span.parent_span_id or fact.parent_span_id,
        "ref": span.ref or fact.ref,
        # 旁白首值为准：重复或迟到的不同旁白不覆盖，也不算结构冲突
        "say": span.say or fact.say,
        "state": state,
        "ended_at": ended_at,
        "end_reason": end_reason,
        "current_attempt_id": attempt,
        "attempts": attempts,
        "incomplete": incomplete or dropped,
    })


def _conflicts(span: SpanRecord, fact: SpanFact) -> bool:
    """已确定的结构字段与新事实不一致。"""
    return any((
        fact.kind is not None and fact.kind != span.kind,
        _differs(span.parent_span_id, fact.parent_span_id),
        _differs(span.ref, fact.ref),
    ))


def _differs(known: str | None, incoming: str | None) -> bool:
    """双方都有值且不同才算冲突。"""
    return known is not None and incoming is not None and known != incoming


def _merge_usage(
    attempts: tuple[AttemptUsage, ...], usage: AttemptUsage | None, limit: int | None,
) -> tuple[tuple[AttemptUsage, ...], bool]:
    """同尝试覆盖、新尝试追加；超过上限时不追加并报告丢弃。"""
    if usage is None:
        return attempts, False
    for index, existing in enumerate(attempts):
        if existing.attempt_id == usage.attempt_id:
            return attempts[:index] + (usage,) + attempts[index + 1:], False
    if limit is not None and len(attempts) >= limit:
        return attempts, True
    return (*attempts, usage), False

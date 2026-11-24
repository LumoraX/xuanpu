"""按写入方序号把一批事实应用到一条 trace：memory 与 PostgreSQL 实现共用，保证同一行为。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.engine.tracing.models import MAX_SOURCES, source_id
from server.engine.tracing.rules import FactRejectedError, apply_fact, new_span

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from server.engine.tracing.models import SpanFact, SpanRecord, TraceRecord


@dataclass(frozen=True)
class AppliedBatch:
    """一批事实的结果：新的 trace 头、变化过的 span、应用后的 span 总数。"""

    record: TraceRecord
    changed: dict[str, SpanRecord]
    span_count: int


def apply_batch(
    record: TraceRecord,
    known: Mapping[str, SpanRecord],
    span_count: int,
    facts: Sequence[SpanFact],
    *,
    source: str,
    max_spans: int,
    max_attempts: int,
) -> AppliedBatch:
    """去重、跳号、上限与被拒事实只影响完整性标记，不抛给采集器。

    序号按写入方 ``source`` 各自连续：同一写入方已应用的序号跳过（重复投递不重复记），
    跳号把 trace 标为不完整；不同写入方互不影响。写入方数量超过上限时整批丢弃并标不完整。
    ``known`` 至少包含本批涉及的已有 span；不在其中的视为新 span。
    """
    source_id(source)
    if source not in record.sources and len(record.sources) >= MAX_SOURCES:
        return AppliedBatch(record=record.model_copy(update={"incomplete": True}),
                            changed={}, span_count=span_count)
    changed: dict[str, SpanRecord] = {}
    sources = dict(record.sources)
    for fact in sorted(facts, key=lambda item: item.seq):
        previous = sources.get(source, 0)
        if fact.seq <= previous:
            continue
        incomplete = record.incomplete or fact.seq != previous + 1
        truncated = record.truncated
        existing = changed.get(fact.span_id) or known.get(fact.span_id)
        try:
            if existing is not None:
                changed[fact.span_id] = apply_fact(existing, fact, max_attempts=max_attempts)
            elif span_count >= max_spans:
                truncated = incomplete = True
            else:
                changed[fact.span_id] = new_span(fact)
                span_count += 1
        except FactRejectedError:
            incomplete = True
        sources[source] = fact.seq
        record = record.model_copy(update={
            "last_seq": record.last_seq + 1, "sources": dict(sources),
            "incomplete": incomplete, "truncated": truncated,
            "updated_at": max(record.updated_at, fact.at),
        })
    return AppliedBatch(record=record, changed=changed, span_count=span_count)

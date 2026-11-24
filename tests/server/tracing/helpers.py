"""追踪测试的共用构造器。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from server.engine.tracing.models import AttemptUsage, SpanFact, TraceOwner

T0 = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
BOB = TraceOwner(tenant_id="acme", user_id="usr_bob")
OTHER_TENANT_ALICE = TraceOwner(tenant_id="globex", user_id="usr_alice")


def at(ms: int) -> datetime:
    """从 T0 起的毫秒偏移。"""
    return T0 + timedelta(milliseconds=ms)


def fact(seq: int, span_id: str, ms: int = 0, **fields: object) -> SpanFact:
    """构造一条事实。"""
    return SpanFact(seq=seq, span_id=span_id, at=at(ms), **fields)  # type: ignore[arg-type]


def usage(attempt: str, inp: int | None, out: int | None, *, complete: bool = True) -> AttemptUsage:
    """构造一次尝试的用量。"""
    return AttemptUsage(attempt_id=attempt, input_tokens=inp, output_tokens=out, usage_complete=complete)

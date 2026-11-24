"""span 状态转换规则：幂等、终态不复活、按尝试计量、结构冲突标不完整。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.engine.tracing.models import SpanFact, TraceRecord
from server.engine.tracing.rules import FactRejectedError, apply_fact, new_span

from .helpers import fact, usage


def _artisan(state: str = "running", attempt: str = "a1"):
    """一个运行中的匠人 span。"""
    return new_span(fact(1, "sp_art", kind="artisan", name="工单助手", parent_span_id="sp_dis",
                         attempt_id=attempt, state=state))


def test_new_span_requires_kind_and_name() -> None:
    """不认识的 span 只有状态没有类型时拒绝，交给存储标不完整。"""
    with pytest.raises(FactRejectedError):
        new_span(fact(1, "sp_x", state="running"))


def test_same_snapshot_twice_does_not_double_count() -> None:
    """同一尝试的重复用量快照覆盖而非累加。"""
    span = _artisan()
    for _ in range(3):
        span = apply_fact(span, fact(2, "sp_art", usage=usage("a1", 100, 20)), max_attempts=8)
    assert (span.input_tokens, span.output_tokens) == (100, 20)


def test_attempts_are_summed_and_unknown_is_not_zero() -> None:
    """不同尝试相加；任一尝试缺用量时总量未知。"""
    span = apply_fact(_artisan(), fact(2, "sp_art", usage=usage("a1", 100, 20)), max_attempts=8)
    span = apply_fact(span, fact(3, "sp_art", attempt_id="a2", state="running",
                                 usage=usage("a2", 50, 5)), max_attempts=8)
    assert (span.input_tokens, span.output_tokens, span.usage_complete) == (150, 25, True)
    span = apply_fact(span, fact(4, "sp_art", usage=usage("a3", None, 1, complete=False)), max_attempts=8)
    assert span.input_tokens is None
    assert span.usage_complete is False


def test_terminal_state_is_not_revived_by_same_attempt() -> None:
    """挂起与终态后同一尝试的迟到进度不复活节点。"""
    span = apply_fact(_artisan(), fact(2, "sp_art", 500, state="completed", end_reason="done"),
                      max_attempts=8)
    late = apply_fact(span, fact(3, "sp_art", 600, attempt_id="a1", state="running"), max_attempts=8)
    assert late.state == "completed"
    assert late.ended_at == span.ended_at


def test_new_attempt_can_run_again() -> None:
    """恢复后的新尝试重新进入运行态，结束时间清空。"""
    span = apply_fact(_artisan(), fact(2, "sp_art", 500, state="waiting_input"), max_attempts=8)
    span = apply_fact(span, fact(3, "sp_art", 600, state="failed", end_reason="model_error"),
                      max_attempts=8)
    resumed = apply_fact(span, fact(4, "sp_art", 900, attempt_id="a2", state="running"), max_attempts=8)
    assert resumed.state == "running"
    assert resumed.ended_at is None
    assert resumed.end_reason is None


def test_cancel_request_is_not_cancelled() -> None:
    """规则里没有“请求取消”状态：只有真实终态事实才进入 cancelled。"""
    with pytest.raises(ValidationError):
        SpanFact.model_validate({"seq": 1, "span_id": "s", "at": "2026-11-14T00:00:00Z",
                                 "state": "cancelling"})


def test_structural_conflict_marks_incomplete_without_overwrite() -> None:
    """类型或父级与已知不同：保留原值并标不完整。"""
    span = apply_fact(_artisan(), fact(2, "sp_art", kind="tool", parent_span_id="sp_other"),
                      max_attempts=8)
    assert span.kind == "artisan"
    assert span.parent_span_id == "sp_dis"
    assert span.incomplete


def test_attempt_cap_drops_and_flags() -> None:
    """尝试数超上限时不追加并标不完整。"""
    span = apply_fact(_artisan(), fact(2, "sp_art", usage=usage("a1", 1, 1)), max_attempts=1)
    span = apply_fact(span, fact(3, "sp_art", usage=usage("a2", 1, 1)), max_attempts=1)
    assert len(span.attempts) == 1
    assert span.incomplete


@pytest.mark.parametrize("bad", ["", "has space", "x" * 161, "a/b"])
def test_identifiers_reject_free_text(bad: str) -> None:
    """标识字段不能夹带正文或路径。"""
    with pytest.raises(ValidationError):
        fact(1, bad, kind="tool", name="t")


def test_names_are_cleaned_and_bounded() -> None:
    """显示名去控制字符、截断；结束原因只能是代码。"""
    span = new_span(fact(1, "sp", kind="tool", name="\x1b[31mgrep\x07" + "x" * 300))
    assert "\x1b" not in span.name and len(span.name) == 120
    with pytest.raises(ValidationError):
        fact(1, "sp", end_reason="出错了，请重试")


def test_record_has_no_content_fields() -> None:
    """span 与 trace 记录没有存放正文、参数或输出的字段。"""
    forbidden = {"arguments", "output", "delta", "summary", "prompt", "text", "content"}
    from server.engine.tracing.models import SpanRecord

    assert not forbidden & set(SpanRecord.model_fields)
    assert not forbidden & set(TraceRecord.model_fields)
    assert not forbidden & set(SpanFact.model_fields)

"""瀑布与深度：父子层级、运行中不编造时长、缺父与成环有界。"""

from __future__ import annotations

from xuanpu_server.tracing.models import TraceRecord, TraceView
from xuanpu_server.tracing.rules import apply_fact, new_span
from xuanpu_server.tracing.waterfall import waterfall

from .helpers import ALICE, at, fact, usage


def _view(*spans) -> TraceView:
    """包成只读视图。"""
    trace = TraceRecord(trace_id="trc", owner=ALICE, origin="user", title="t",
                        created_at=at(0), updated_at=at(0))
    return TraceView(trace=trace, spans=spans)


def test_parallel_artisans_overlap_under_dispatch() -> None:
    """管家 → 派工 → 两个并行匠人；匠人行有真实交叠。"""
    butler = apply_fact(new_span(fact(1, "b", 0, kind="butler", name="管家", state="running")),
                        fact(9, "b", 900, state="completed"), max_attempts=4)
    dispatch = new_span(fact(2, "d", 100, kind="dispatch", name="派工", parent_span_id="b"))
    left = apply_fact(
        new_span(fact(3, "l", 150, kind="artisan", name="工单", parent_span_id="d", state="running")),
        fact(7, "l", 600, state="completed", usage=usage("a1", 100, 10)), max_attempts=4)
    right = new_span(fact(4, "r", 200, kind="artisan", name="报表", parent_span_id="d", state="running"))
    rows = {row.span_id: row for row in waterfall(_view(butler, dispatch, left, right))}
    assert [rows[k].depth for k in "bdlr"] == [0, 1, 2, 2]
    assert rows["l"].offset_ms == 150 and rows["l"].duration_ms == 450
    assert rows["r"].duration_ms is None
    assert rows["r"].offset_ms < rows["l"].offset_ms + rows["l"].duration_ms
    assert rows["l"].input_tokens == 100 and rows["r"].input_tokens is None


def test_missing_parent_and_cycle_are_orphans() -> None:
    """缺父与成环不递归挂死，记为深度 0 的孤儿。"""
    lost = new_span(fact(1, "x", 0, kind="tool", name="grep", parent_span_id="nowhere"))
    a = new_span(fact(2, "a", 10, kind="artisan", name="甲", parent_span_id="c"))
    c = new_span(fact(3, "c", 20, kind="artisan", name="丙", parent_span_id="a"))
    rows = {row.span_id: row for row in waterfall(_view(lost, a, c))}
    assert all(row.orphan and row.depth == 0 for row in rows.values())


def test_empty_trace_has_no_rows() -> None:
    """空 trace 不报错。"""
    assert waterfall(_view()) == ()

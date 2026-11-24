"""一条 trace 的多写入方：各自序号连续，互不误判跳号；同写入方的重复与跳号照旧处理。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.engine.tracing.models import MAX_SOURCES

from .helpers import ALICE, fact
from .test_span_store import _open, store  # noqa: F401 —— 复用同一夹具（memory 与 PostgreSQL）

if TYPE_CHECKING:
    from server.engine.tracing.store import SpanStore


async def test_interleaved_sources_do_not_look_like_gaps(store: SpanStore) -> None:  # noqa: F811
    """两个写入方交错写入、各从 1 开始：不标不完整，last_seq 是应用总数。"""
    await _open(store)
    await store.apply(ALICE, "trc_1", [fact(1, "root", kind="butler", name="管家")], source="a")
    await store.apply(ALICE, "trc_1", [fact(1, "dp", kind="dispatch", name="派工",
                                            parent_span_id="root")], source="b")
    await store.apply(ALICE, "trc_1", [fact(2, "root", state="running")], source="a")
    record = await store.apply(ALICE, "trc_1", [fact(2, "dp", state="running")], source="b")
    assert not record.incomplete
    assert record.sources == {"a": 2, "b": 2} and record.last_seq == 4


async def test_duplicates_and_gaps_are_per_source(store: SpanStore) -> None:  # noqa: F811
    """同写入方重复的序号跳过、不计数；某写入方跳号才标不完整。"""
    await _open(store)
    await store.apply(ALICE, "trc_1", [fact(1, "root", kind="butler", name="管家")], source="a")
    again = await store.apply(ALICE, "trc_1", [fact(1, "root", state="failed")], source="a")
    assert again.last_seq == 1 and not again.incomplete
    view = await store.get(ALICE, "trc_1")
    assert view.spans[0].state == "queued"
    gap = await store.apply(ALICE, "trc_1", [fact(3, "root", state="running")], source="b")
    assert gap.incomplete and gap.sources["b"] == 3


async def test_default_source_keeps_single_writer_behaviour(store: SpanStore) -> None:  # noqa: F811
    """不给写入方时沿用单写入方语义。"""
    await _open(store)
    record = await store.apply(ALICE, "trc_1", [fact(1, "root", kind="artisan", name="x"),
                                                fact(2, "root", state="running")])
    assert record.sources == {"main": 2} and not record.incomplete


async def test_too_many_sources_marks_incomplete(store: SpanStore) -> None:  # noqa: F811
    """写入方数量有上限，超出的批次丢弃并标不完整。"""
    await _open(store)
    for index in range(MAX_SOURCES):
        full = await store.apply(ALICE, "trc_1", [fact(1, "shared", kind="tool", name="t")],
                                 source=f"w{index}")
    assert not full.incomplete and len(full.sources) == MAX_SOURCES
    record = await store.apply(ALICE, "trc_1", [fact(1, "late", kind="tool", name="t")],
                               source="overflow")
    assert record.incomplete and "overflow" not in record.sources

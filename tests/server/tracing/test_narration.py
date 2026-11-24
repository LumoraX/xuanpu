"""执行旁白（R7-1）：匠人在调用工具前对员工说的那句话挂到工具 span 的 ``say`` 上。

只取 ``assistant_text``；推理（``assistant_reasoning``）、工具参数与输出一律不进 trace。
"""

from __future__ import annotations

from datetime import UTC, datetime

import taifeng

from server.engine.tracing.collector import TraceCollector
from server.engine.tracing.config import ExecutionTraceConfig
from server.engine.tracing.memory import MemorySpanStore
from server.engine.tracing.narration import SAY_MAX, narration, without_narration
from server.engine.tracing.rules import apply_fact, new_span
from server.engine.tracing.views import span_view

from .helpers import ALICE, fact

CONFIG = ExecutionTraceConfig(enabled=True, tenants=("acme",))
IDENTITY = {"execution_id": "ex1", "attempt_id": "at1", "thread_id": "th1", "skill_id": "报销核对"}


def _event(kind: str, data: dict[str, object]) -> taifeng.EventMsg:
    """同一执行上的一条内核事件。"""
    return taifeng.EventMsg.model_validate({
        "submission_id": "sub_1", "msg": {"kind": kind, "data": data},
        "timestamp": datetime.now(UTC), "seq": 1, "execution": IDENTITY})


def _tool(call: str) -> taifeng.EventMsg:
    """一次工具调用开始；参数故意带敏感内容，确认不会进 trace。"""
    return _event("tool_call_started", {"call_id": call, "name": "query_reimbursements",
                                        "arguments": '{"token": "secret"}'})


async def _says(events: list[taifeng.EventMsg]) -> dict[str, str | None]:
    """跑一遍采集器，返回每个 span 的旁白。"""
    store = MemorySpanStore()
    collector = TraceCollector(store, ALICE, CONFIG, ref="ses_1")
    for event in [_event("turn_started", {}), *events]:
        collector.handle(event)
    await collector.flush()
    page = await store.list(ALICE)
    view = await store.get(ALICE, page.items[0].trace_id)
    return {span.span_id: span.say for span in view.spans}


def test_narration_keeps_whole_line_when_it_fits() -> None:
    """放得下就整段保留：真实模型把「做什么」与「不做什么」拆成两句时，两句都要。"""
    said = "我先只读查询工单 T-1001 的状态。不会修改工单或优先级。"
    assert narration(said) == said


def test_narration_keeps_trailing_sentences_when_too_long() -> None:
    """放不下时按整句从结尾往前保留，开头的长铺垫丢掉。"""
    said = narration("铺垫" * 100 + "。我先去档案柜翻上月的报销单。只读，不改。")
    assert said == "我先去档案柜翻上月的报销单。只读，不改。"


def test_narration_strips_think_blocks_and_whitespace() -> None:
    """推理标签整块去掉，换行折成空格。"""
    assert narration("<think>先想想</think>\n查一下\n流水") == "查一下 流水"


def test_narration_empty_is_none() -> None:
    """只有空白时没有旁白。"""
    assert narration("  \n ") is None
    assert narration("<think>只有推理</think>") is None


def test_narration_truncates_from_the_front() -> None:
    """一句话太长时保留结尾（离动作最近的部分），开头标省略号。"""
    said = narration("甲" * 400)
    assert said is not None and len(said) == SAY_MAX and said.startswith("…")


async def test_say_goes_to_first_tool_only() -> None:
    """先说话再并行调两个工具：旁白只挂第一个，第二个没有，不重复。"""
    says = await _says([
        _event("assistant_text", {"delta": "我先去档案柜，"}),
        _event("assistant_text", {"delta": "翻上月的报销单。"}),
        _tool("c1"), _tool("c2"),
    ])
    assert says["tl.c1"] == "我先去档案柜，翻上月的报销单。"
    assert says["tl.c2"] is None
    assert says["ex.ex1"] is None


async def test_reasoning_and_arguments_never_become_say() -> None:
    """推理增量与工具参数都不进旁白。"""
    says = await _says([
        _event("assistant_reasoning", {"delta": "内部推理不该外露"}),
        _tool("c1"),
    ])
    assert says["tl.c1"] is None


async def test_turn_end_clears_buffer() -> None:
    """一轮结束后剩下的话是回复，不挂到下一轮的工具上。"""
    says = await _says([
        _event("assistant_text", {"delta": "两笔没付，合计 3280 元。"}),
        _event("turn_suspended", {}),
        _tool("c9"),
    ])
    assert says["tl.c9"] is None


def test_first_say_wins_without_conflict() -> None:
    """已有旁白时，重复或迟到的不同旁白不覆盖、也不标不完整。"""
    span = new_span(fact(1, "tl.c1", kind="tool", name="q", parent_span_id="ex.1",
                         state="running", say="先查档案"))
    later = apply_fact(span, fact(2, "tl.c1", say="另一句"), max_attempts=None)
    assert later.say == "先查档案" and not later.incomplete
    assert span_view(later)["say"] == "先查档案"


def test_unpaired_reasoning_never_shows() -> None:
    """没闭合的推理、被截掉开头的推理、行内的开始标记：推理部分一律不显示（复核发现）。"""
    assert narration("<think>secret reasoning: password is hunter2") is None
    assert narration("x" * 50 + " the api key is abc</think>我去查一下。") == "我去查一下。"
    assert narration("好的<thinking>内部推理") == "好的"
    assert narration("<THINK>a</THINK>我先查工单。<think>b") == "我先查工单。"


def test_buffer_is_bounded_per_collector() -> None:
    """没有结束事件的执行不让缓冲无限增长。"""
    from server.engine.tracing import collector as module

    c = TraceCollector(MemorySpanStore(), ALICE, CONFIG, ref="ses_1")
    for i in range(module._SAID_MEMORY + 10):  # noqa: SLF001
        c._hear(f"ex{i}", "话")  # noqa: SLF001
    assert len(c._said) == module._SAID_MEMORY  # noqa: SLF001
    assert "ex0" not in c._said  # noqa: SLF001


def test_result_drops_narration_lines() -> None:
    """交付给员工的结果去掉每步前说过的话；全去掉了就保留原文（R7 真实试用发现）。"""
    said = ["我先查询工单 T-1001 的当前信息。", "我已查到工单；现在计算时限。"]
    full = "我先查询工单 T-1001 的当前信息。我已查到工单；现在计算时限。\n\n\n工单 T-1001：开放，24 小时内处理。"
    assert without_narration(full, said) == "工单 T-1001：开放，24 小时内处理。"
    assert without_narration(said[0], said) == said[0]
    assert without_narration("结果", []) == "结果"

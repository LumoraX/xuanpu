"""采集器：真实内核事件（脚本化模型）折成持久执行树。

场景照内核 ``execution_tree`` 示例：协调技能并行调用同名 worker 两次与另一技能，worker
先读事实再结束。验证同名执行不合并、父子正确、工具挂在发起者下、用量按尝试记、
下一轮另开 trace，以及写入失败只降为不完整。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
import taifeng
from taifeng import RoutingSimClient, SimTurn
from xuanpu_server.tracing.collector import TraceCollector, tracer_for
from xuanpu_server.tracing.config import ExecutionTraceConfig
from xuanpu_server.tracing.memory import MemorySpanStore
from xuanpu_server.tracing.waterfall import waterfall

from .helpers import ALICE

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_server.tracing.store import SpanStore

CONFIG = ExecutionTraceConfig(enabled=True, tenants=("acme",))
SKILLS = {
    "coordinator": ("composite", True, "[worker, other]", "TREE_COORDINATOR_MARK"),
    "worker": ("composite", False, "[facts]", "TREE_WORKER_MARK"),
    "other": ("atomic", False, None, "TREE_OTHER_MARK"),
    "facts": ("atomic", False, None, "静态事实"),
}


def _skills(root: Path) -> Path:
    """写出四个演示技能。"""
    for name, (kind, entry, children, body) in SKILLS.items():
        lines = ["---", f"name: {name}", f"description: {name} 演示", "version: 1.0.0",
                 f"type: {kind}"]
        if entry:
            lines += ["entry: true", "model: sim-model"]
        if children:
            lines += [f"child_skills: {children}", "max_call_depth: 4"]
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text("\n".join([*lines, "---", f"# {body}", ""]),
                                              encoding="utf-8")
    return root


def _client(turns: int = 1) -> RoutingSimClient:
    """每轮：协调者并行派三个子执行；两个 worker 各读一次事实。调用标识逐轮不同。"""
    coordinator, worker, other = [], [], []
    for turn in range(turns):
        calls = [{"id": f"child_{turn}_{i}", "name": "call_skill",
                  "arguments": json.dumps({"skill_id": skill, "args": {"task": task},
                                           "reason": "并行"})}
                 for i, (skill, task) in enumerate([("worker", "甲"), ("worker", "乙"), ("other", "丙")])]
        coordinator += [SimTurn(tool_calls=calls), SimTurn(text="全部完成。")]
        worker += [SimTurn(tool_calls=[{"id": f"read_{turn}_{i}", "name": "read_skill",
                                        "arguments": '{"skill_id":"facts"}'}]) for i in range(2)]
        worker += [SimTurn(text="第一份完成。"), SimTurn(text="第二份完成。")]
        other.append(SimTurn(text="独立任务完成。"))
    return RoutingSimClient(routes={"TREE_COORDINATOR_MARK": coordinator,
                                    "TREE_WORKER_MARK": worker, "TREE_OTHER_MARK": other})


async def _run_turns(tmp_path: Path, store: SpanStore, turns: int = 1) -> TraceCollector:
    """起一个引擎，接上采集器，跑若干轮。"""
    pool = await taifeng.EnginePool.create(
        skills_dir=_skills(tmp_path / "skills"), threads_dir=tmp_path / "threads",
        model_client=_client(turns), compressors=[], max_parallel_tool_calls=4,
    )
    engine = await pool.get_or_create(session_id="ses_demo", entry_skill_id="coordinator")
    collector = TraceCollector(store, ALICE, CONFIG, ref="ses_demo")
    collector.start()
    events = engine.subscribe_all()
    try:
        for _ in range(turns):
            submission = await engine.submit(taifeng.UserMessage(text="并行整理。"))
            async for event in events:
                collector.handle(event)
                if event.submission_id == submission and event.msg.kind == "turn_completed" \
                        and event.msg.data.get("is_root"):
                    break
    finally:
        await pool.close()
        await collector.close()
    return collector


async def test_parallel_children_form_one_tree(tmp_path: Path) -> None:
    """同名 worker 两个独立节点；父子、工具归属、状态、用量都对。"""
    store = MemorySpanStore()
    await _run_turns(tmp_path, store)
    page = await store.list(ALICE)
    assert page.total == 1
    view = await store.get(ALICE, page.items[0].trace_id)
    assert not view.trace.incomplete and not view.active
    by_kind = {kind: [s for s in view.spans if s.kind == kind] for kind in ("artisan", "tool")}
    root = [s for s in by_kind["artisan"] if s.parent_span_id is None]
    assert len(root) == 1 and root[0].name == "coordinator" and root[0].ref == "ses_demo"
    workers = [s for s in by_kind["artisan"] if s.name == "worker"]
    assert len(workers) == 2 and workers[0].span_id != workers[1].span_id
    assert all(s.parent_span_id == root[0].span_id for s in by_kind["artisan"] if s is not root[0])
    reads = [s for s in by_kind["tool"] if s.name == "read_skill"]
    assert {s.parent_span_id for s in reads} == {w.span_id for w in workers}
    assert all(s.state == "completed" for s in by_kind["artisan"])
    assert all(s.state in ("completed", "failed") for s in by_kind["tool"])
    assert all(s.input_tokens is not None for s in by_kind["artisan"])
    rows = waterfall(view)
    assert {r.depth for r in rows if r.kind == "artisan" and r.name == "worker"} == {1}


async def test_each_turn_is_its_own_trace(tmp_path: Path) -> None:
    """根执行结束即结束一条 trace；第二轮另开。"""
    store = MemorySpanStore()
    await _run_turns(tmp_path, store, turns=2)
    page = await store.list(ALICE)
    assert page.total == 2
    for record in page.items:
        assert not (await store.get(ALICE, record.trace_id)).trace.incomplete


class _Flaky(MemorySpanStore):
    """第一次 apply 失败，之后恢复。"""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def apply(self, *args: object, **kwargs: object):  # type: ignore[override]
        if self.failures:
            self.failures -= 1
            raise ConnectionError("db down")
        return await super().apply(*args, **kwargs)  # type: ignore[arg-type]


async def test_write_failure_degrades_to_incomplete(tmp_path: Path) -> None:
    """一次写入失败不打断对话，trace 最终标为不完整。"""
    store = _Flaky()
    await _run_turns(tmp_path, store)
    page = await store.list(ALICE)
    assert page.total == 1
    assert (await store.get(ALICE, page.items[0].trace_id)).trace.incomplete


@pytest.mark.parametrize(("config", "user", "expected"), [
    (CONFIG, "usr_alice", True),
    (CONFIG, None, False),
    (ExecutionTraceConfig(enabled=True), "usr_alice", False),
    (ExecutionTraceConfig(enabled=True, tenants=("globex",)), "usr_alice", False),
    (None, "usr_alice", False),
])
def test_tracer_only_for_enabled_tenant_with_owner(config, user, expected) -> None:
    """未开启的租户或没有主人的会话不采集。"""

    class _Control:
        trace_store = MemorySpanStore()

    tracer = tracer_for(_Control(), config, "acme", "ses_1", user)  # type: ignore[arg-type]
    assert (tracer is not None) is expected


async def test_handle_never_raises() -> None:
    """坏事件只记日志。"""
    collector = TraceCollector(MemorySpanStore(), ALICE, CONFIG, ref=None)
    collector.handle(object())  # type: ignore[arg-type]
    await asyncio.sleep(0)

"""送回审批的结论：核销事件对到在等结论的那次送回上；事件枢纽关闭时等不到的算失败。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from xuanpu_server.audit import AuditLog
from xuanpu_server.controlplane import InMemoryControlPlane
from xuanpu_server.metrics import PlatformMetrics
from xuanpu_server.runtime.events import EngineEventHub
from xuanpu_server.runtime.gate_audit import (
    ConnectorToolInfo,
    PendingRejections,
    RejectionAudit,
)
from xuanpu_server.runtime.resolutions import ResolutionLedger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class Recorder:
    """记下收到的结论的等待方。"""

    resolved_ids: list[frozenset[str]] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    abandons: int = 0

    async def resolved(self, request_ids: frozenset[str]) -> None:
        """记下核销了哪些。"""
        self.resolved_ids.append(request_ids)

    def refused(self, reason: str) -> None:
        """记下被拒的缘由。"""
        self.refusals.append(reason)

    def abandoned(self) -> None:
        """记一次等不到。"""
        self.abandons += 1


@dataclass
class Msg:
    """事件内容的替身。"""

    kind: str
    data: dict[str, Any]


@dataclass
class Event:
    """内核事件的替身：只有结论处理用得到的两个字段。"""

    submission_id: str
    msg: Msg


def event(submission_id: str, kind: str, **data: Any) -> Any:
    """造一条结论事件。"""
    return Event(submission_id, Msg(kind, {"record_id": "sr_1", **data}))


async def test_resolved_lists_only_what_this_submission_newly_settled() -> None:
    """部分核销给的是这一次核销的；整个挂起处理完给的是全部，减去之前部分核销过的。
    部分核销期间已核销的再被送回、内核又列进去，也不算第二次。"""
    ledger = ResolutionLedger("ses_1")
    first, again, last = Recorder(), Recorder(), Recorder()
    ledger.watch("sub_a", first)
    ledger.watch("sub_b", again)
    ledger.watch("sub_c", last)
    await ledger.settle(
        event("sub_a", "suspension_partially_resolved", resolved_request_ids=["r1"])
    )
    await ledger.settle(
        event("sub_b", "suspension_partially_resolved", resolved_request_ids=["r1"])
    )
    await ledger.settle(event("sub_c", "suspension_resolved", request_ids=["r1", "r2"]))
    assert first.resolved_ids == [frozenset({"r1"})]
    assert again.resolved_ids == [frozenset()]
    assert last.resolved_ids == [frozenset({"r2"})]


async def test_refused_ended_and_closed() -> None:
    """内核整次拒绝告诉等待方缘由；没有结论就结束、或枢纽关闭，告诉等待方等不到了。

    结论事件缺字段同样算等不到。没人在等的结论不出错。
    """
    ledger = ResolutionLedger("ses_1")
    refused, ended, closed, broken = Recorder(), Recorder(), Recorder(), Recorder()
    for submission_id, watch in [
        ("sub_a", refused), ("sub_b", ended), ("sub_c", closed), ("sub_d", broken),
    ]:  # fmt: skip
        ledger.watch(submission_id, watch)
    await ledger.settle(
        event("sub_a", "suspension_resolve_rejected", reason="no_active_suspension")
    )
    ledger.ended("sub_b")
    await ledger.settle(event("sub_d", "suspension_resolved"))
    await ledger.settle(event("sub_x", "suspension_resolved", request_ids=["r1"]))
    ledger.close()
    assert refused.refusals == ["no_active_suspension"]
    assert (ended.abandons, closed.abandons, broken.abandons) == (1, 1, 1)
    assert refused.abandons == 0


class SilentEngine:
    """一条事件都不发的引擎替身：只用来起一个事件枢纽。"""

    def subscribe_all_envelopes(self) -> AsyncIterator[Any]:
        """一个永远等不到下一条的订阅。"""

        async def never() -> AsyncIterator[Any]:
            await asyncio.Event().wait()
            yield None

        return never()


async def test_hub_closing_before_the_kernel_answers_counts_as_unconfirmed() -> None:
    """送回提交之后、内核的结论到来之前事件枢纽就关了：被拒的调用补不上，按 unconfirmed 计入。"""
    store = InMemoryControlPlane()
    metrics = PlatformMetrics(
        sessions_live=lambda: 0, turns_in_flight=lambda: 0, runtimes_live=lambda: 0
    )
    audit = AuditLog(store, metrics=metrics)
    hub = EngineEventHub(
        SilentEngine(),  # type: ignore[arg-type]
        tenant_id="acme",
        session_id="ses_1",
        audit=audit,
        store=store,
        metrics=metrics,
    )
    await hub.start()
    rejections = RejectionAudit(store=store, audit=audit, on_failure=metrics.backfill_failed)
    item = {
        "request_id": "r1",
        "reason": "permission",
        "detail": {
            "scope": "tool_use",
            "target": "mcp__crm__update_ticket",
            "call_id": "c1",
            "thread_id": "thr_1",
            "args": {"id": "T-1"},
        },
    }
    pending = PendingRejections(
        tenant_id="acme", session_id="ses_1", submission_id="sub_s", requests=(item,)
    )
    info = ConnectorToolInfo(
        connector="crm", transport="stdio", tool="update_ticket", effect="write"
    )
    watch = rejections.watch(pending, {"mcp__crm__update_ticket": info}.get)
    assert watch is not None
    hub.watch_resolution("sub_r", watch)
    await hub.close()
    assert rejections.failures == 1
    text = metrics.render().decode("utf-8")
    assert 'xuanpu_gate_backfill_failures_total{stage="unconfirmed"} 1.0' in text

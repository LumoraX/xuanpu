"""每次请求的预算随任务提高（R6-C，ADR 0020）：管家说明理由提高，有上限，只对这次请求生效，留痕。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from server.core.controlplane.models import AuditQuery
from server.engine.butler.config import ButlerConfig
from server.engine.butler.limits import budget_config
from server.engine.butler.service import ButlerMessageBody
from server.engine.tracing.models import TraceOwner

from .conftest import employee, enable

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
BOB = TraceOwner(tenant_id="acme", user_id="usr_bob")
INSTRUCTION = "INSTR-查询工单 T-1 的进度"


def _dispatch(call_id: str) -> tuple[str, str, dict[str, Any]]:
    """管家派给样例匠人。"""
    return (call_id, "dispatch_artisan",
            {"package": "support-desk", "entry": "triage", "instruction": INSTRUCTION})


def _raise(call_id: str, budget: str, to: int, reason: str = "要查的工单比预想多") -> tuple[
        str, str, dict[str, Any]]:
    """管家提高预算。"""
    return (call_id, "raise_budget", {"budget": budget, "to": to, "reason": reason})


async def _raises(world: Any) -> list[dict[str, Any]]:
    """提高预算的审计记录。"""
    records = await world.platform.store.query_audit(
        "acme", AuditQuery(kind="butler_budget_raised", limit=50))
    return [dict(r.data) for r in records]


def test_ceilings_cannot_be_below_starting_budgets() -> None:
    """上限小于起始值是配置错误；默认起始值不变，上限更宽。"""
    with pytest.raises(ValidationError):
        ButlerConfig(max_tokens_per_trace=500_000, max_tokens_per_trace_ceiling=400_000)
    config = ButlerConfig()
    assert config.max_tokens_per_trace == 400_000 and config.max_tokens_per_trace_ceiling == 20_000_000


async def test_raised_artisan_budget_lets_the_second_dispatch_through(world: Any, step: Any) -> None:
    """起始只允许一个匠人：管家说明理由提高到 2 后，第二次派工成功；记审计带理由。"""
    enable(world, max_artisans_per_trace=1)
    await world.publish()
    alice = await employee(world)
    world.models.script_for("INSTR-", step(text="好的"), step(text="好的"))
    world.models.script(step(calls=[_dispatch("c1")]), step(calls=[_raise("c2", "artisans", 2)]),
                        step(calls=[_dispatch("c3")]), step(text="已派出两位匠人。"))
    receipt = await world.platform.butler.message(
        alice, ButlerMessageBody(request_id="b1", text="分两路查工单"))
    assert len(receipt["dispatches"]) == 2
    [record] = await _raises(world)
    assert record["budget"] == "artisans" and record["from"] == 1 and record["limit"] == 2
    assert record["reason"] == "要查的工单比预想多" and record["trace_id"] == receipt["trace_id"]
    effective = await budget_config(world.platform, ALICE, receipt["trace_id"])
    assert effective.max_artisans_per_trace == 2


async def test_raise_is_capped_scoped_and_never_lowers(world: Any, step: Any) -> None:
    """超过上限按上限；只对这次请求、这名员工生效；要求更低不改。"""
    enable(world, max_tokens_per_trace=1_000, max_tokens_per_trace_ceiling=5_000)
    await world.publish()
    alice = await employee(world)
    await employee(world, "usr_bob")
    world.models.script(step(calls=[_raise("c1", "tokens", 1_000_000)]),
                        step(calls=[_raise("c2", "tokens", 2_000)]), step(text="好的。"))
    receipt = await world.platform.butler.message(
        alice, ButlerMessageBody(request_id="b2", text="这件事很大"))
    [record] = await _raises(world)
    assert record["limit"] == 5_000 and record["requested"] == 1_000_000
    assert '"raised": false' in str(world.models.requests[-1])
    trace = receipt["trace_id"]
    assert (await budget_config(world.platform, ALICE, trace)).max_tokens_per_trace == 5_000
    assert (await budget_config(world.platform, ALICE, "trc_other")).max_tokens_per_trace == 1_000
    assert (await budget_config(world.platform, BOB, trace)).max_tokens_per_trace == 1_000


async def test_raise_needs_a_reason_and_a_known_budget(world: Any, step: Any) -> None:
    """没有理由或未知预算项：工具报错，不提高、不留记录。"""
    enable(world)
    await world.publish()
    alice = await employee(world)
    world.models.script(step(calls=[_raise("c1", "tokens", 900_000, reason=" ")]),
                        step(calls=[("c2", "raise_budget", {"budget": "daily_quota", "to": 10,
                                                            "reason": "想要更多"})]),
                        step(text="好的。"))
    await world.platform.butler.message(alice, ButlerMessageBody(request_id="b3", text="试试"))
    assert "reason 必填" in str(world.models.requests[-1])
    assert await _raises(world) == []

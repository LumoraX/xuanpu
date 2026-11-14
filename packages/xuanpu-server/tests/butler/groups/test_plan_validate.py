"""计划严格解析与服务端校验：不合规一律报错且不建编组、不派工。"""

from __future__ import annotations

from typing import Any

import pytest
from xuanpu_server.butler.budget import ButlerBudgetError, TraceUsage
from xuanpu_server.butler.config import ButlerConfig
from xuanpu_server.butler.groups.plan import (
    GroupRecord,
    MemberRun,
    next_step,
    step_outcome,
)
from xuanpu_server.butler.groups.validate import check_group_budget, parse_plan
from xuanpu_server.errors import InvalidRequestError

from .helpers import ALICE, member, plan, submit

CONFIG = ButlerConfig(enabled=True, tenants=("acme",), max_group_members=2, max_group_steps=2,
                      max_artisans_per_trace=3)


@pytest.mark.parametrize("raw", [
    plan({"mode": "parallel", "members": [member("A")], "extra": 1}),
    plan({"mode": "consult", "members": [member("A")]}),
    plan({"mode": "parallel", "members": [member("A")], "when": "completed"}),
    plan({"mode": "parallel", "members": [member("A")]},
         {"mode": "relay", "members": [member("B")], "when": "maybe"}),
    plan({"mode": "parallel", "members": []}),
    plan({"mode": "parallel", "members": [{**member("A"), "script": "rm -rf /"}]}),
    plan({"mode": "parallel", "members": [{**member("A"), "package": "../etc"}]}),
    {"title": "x", "steps": []},
    {"steps": [{"mode": "parallel", "members": [member("A")]}]},
])
def test_strict_parsing_rejects(raw: dict[str, Any]) -> None:
    """未知字段、会诊（D1 才开放）、第一步带 when、未知终态、空成员、越界引用都拒绝。"""
    with pytest.raises(InvalidRequestError):
        parse_plan(raw, CONFIG)


def test_limits_and_budget() -> None:
    """步数、每步成员数上限；成员总数加已有派工不能超过匠人上限。"""
    three_steps = plan(*[{"mode": "parallel", "members": [member("A")]}] * 3)
    with pytest.raises(InvalidRequestError, match="最多 2 步"):
        parse_plan(three_steps, CONFIG)
    crowded = plan({"mode": "parallel", "members": [member("A"), member("B"), member("C")]})
    with pytest.raises(InvalidRequestError, match="最多 2 个成员"):
        parse_plan(crowded, CONFIG)
    two = parse_plan(plan({"mode": "parallel", "members": [member("A"), member("B")]}), CONFIG)
    check_group_budget(CONFIG, two, TraceUsage(artisans=1))
    with pytest.raises(ButlerBudgetError):
        check_group_budget(CONFIG, two, TraceUsage(artisans=2))
    with pytest.raises(ButlerBudgetError):
        check_group_budget(CONFIG, two, TraceUsage(tokens=None))


def _record(raw: dict[str, Any], runs: tuple[MemberRun, ...], step: int = 0) -> GroupRecord:
    """构造编组以测纯函数。"""
    from datetime import UTC, datetime

    t0 = datetime(2026, 11, 14, tzinfo=UTC)
    return GroupRecord(group_id="grp_1", tenant_id="acme", user_id="usr_alice", request_id="r",
                       input_fingerprint="0" * 64, trace_id="trc_1", plan=parse_plan(raw, CONFIG),
                       runs=runs, step=step, created_at=t0, updated_at=t0)


def test_step_outcomes_and_when() -> None:
    """并行要全部出结果；接力任一失败即失败；when 只认前一步终态，不符的步跳过。"""
    parallel = plan({"mode": "parallel", "members": [member("A"), member("B")]},
                    {"mode": "relay", "members": [member("C")], "when": "completed"})
    half = _record(parallel, (MemberRun(step=0, index=0, outcome="completed"),))
    assert step_outcome(half, 0) is None
    both = _record(parallel, (MemberRun(step=0, index=0, outcome="completed"),
                              MemberRun(step=0, index=1, outcome="failed")))
    assert step_outcome(both, 0) == "failed"
    assert next_step(both, "failed") == (None, ("failed", "skipped"))
    relay = plan({"mode": "relay", "members": [member("A"), member("B")]})
    stopped = _record(relay, (MemberRun(step=0, index=0, outcome="failed"),))
    assert step_outcome(stopped, 0) == "failed"


async def test_unavailable_member_or_foreign_material_creates_nothing(
    butler: dict[str, Any], step: Any,
) -> None:
    """成员匠人不在本人清单、材料不是本次提供：工具报错，不建编组、不派工。"""
    world = butler["world"]
    bad_member = plan({"mode": "parallel", "members": [
        member("A"), {"package": "finance", "entry": "pay", "brief": "付款"}]})
    bad_material = plan({"mode": "parallel", "members": [
        {**member("A"), "materials": [{"item_id": "itm_x", "revision": 1}]}]})
    for index, raw in enumerate((bad_member, bad_material)):
        receipt = await submit(butler, step, f"v{index}", raw)
        assert receipt["groups"] == [] and receipt["dispatches"] == []
        assert await world.platform.dispatches.list_for_trace(ALICE, receipt["trace_id"]) == ()

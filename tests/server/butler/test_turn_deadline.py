"""一轮的动态时限（R6-B，ADR 0020）：默认时限、管家说明理由后延长、上限截断、到时限正常收尾。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from server.core.controlplane.models import AuditQuery
from server.engine.butler.config import ButlerConfig


def _limits(butler: dict[str, Any], **values: Any) -> None:
    """测试用的亚秒时限：绕过配置下限（只在测试里这样装配）。"""
    base = ButlerConfig(enabled=True, tenants=("acme",)).model_dump()
    butler["world"].platform.butler_config = ButlerConfig.model_construct(**{**base, **values})


async def _say(butler: dict[str, Any], text: str, request_id: str) -> dict[str, Any]:
    """以 Alice 身份对管家说一句话。"""
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": request_id, "text": text})
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _extensions(butler: dict[str, Any]) -> list[dict[str, Any]]:
    """延长时限的审计记录。"""
    records = await butler["world"].platform.store.query_audit(
        "acme", AuditQuery(kind="butler_turn_extended", limit=50))
    return [dict(r.data) for r in records]


async def _release_after(gate: asyncio.Event, seconds: float) -> None:
    """过一会儿放行卡住的采样。"""
    await asyncio.sleep(seconds)
    gate.set()


def test_ceiling_cannot_be_below_default() -> None:
    """上限小于默认时限是配置错误。"""
    with pytest.raises(ValidationError):
        ButlerConfig(enabled=True, tenants=("acme",), turn_seconds=600, max_turn_seconds=300)
    assert ButlerConfig().turn_seconds == 300 and ButlerConfig().max_turn_seconds == 86_400


async def test_turn_that_runs_out_of_time_ends_normally(butler: dict[str, Any], step: Any) -> None:
    """到时限：不报 503，回复说明这一轮先停下，且执行追踪根 span 正常收尾而不标不完整。"""
    _limits(butler, turn_seconds=0.3, max_turn_seconds=1.0, reply_window_seconds=5.0)
    butler["world"].models.script(step(text="", wait=asyncio.Event()))
    receipt = await _say(butler, "把十年的账都核一遍", "t1")
    assert receipt["state"] == "done" and "已到时限先停下" in receipt["message"]
    trace_resp = await butler["http"].get(f"/v1/traces/{receipt['trace_id']}")
    assert trace_resp.status_code == 200, trace_resp.text
    body = trace_resp.json()
    assert body["incomplete"] is False
    assert body["active"] is False
    roots = [s for s in body["spans"] if s["kind"] == "butler"]
    assert len(roots) == 1
    assert roots[0]["state"] == "cancelled"
    assert roots[0]["end_reason"] == "turn_timeout"


async def test_butler_extends_its_own_deadline_with_a_reason(
    butler: dict[str, Any], step: Any,
) -> None:
    """管家说明理由延长：这一轮跑过默认时限仍正常完成；延长记审计并带理由。"""
    _limits(butler, turn_seconds=0.3, max_turn_seconds=30.0, reply_window_seconds=5.0)
    gate = asyncio.Event()
    butler["world"].models.script(
        step(calls=[("c1", "extend_turn", {"seconds": 5, "reason": "要逐条核对 40 份合同"})]),
        step(text="合同核对完了。", wait=gate))
    release = asyncio.create_task(_release_after(gate, 0.8))
    receipt = await _say(butler, "核对合同", "t2")
    await release
    assert receipt["message"] == "合同核对完了。"
    [record] = await _extensions(butler)
    assert record["reason"] == "要逐条核对 40 份合同" and record["capped"] is False


async def test_extension_is_capped_at_the_ceiling(butler: dict[str, Any], step: Any) -> None:
    """要得再多也只到上限：如实告诉模型已封顶，到上限照常收尾。"""
    _limits(butler, turn_seconds=0.2, max_turn_seconds=0.6, reply_window_seconds=5.0)
    butler["world"].models.script(
        step(calls=[("c1", "extend_turn", {"seconds": 3600, "reason": "需要很久"})]),
        step(text="", wait=asyncio.Event()))
    receipt = await _say(butler, "做一件很长的事", "t3")
    assert "已到时限先停下" in receipt["message"]
    [record] = await _extensions(butler)
    assert record["capped"] is True and record["remaining_seconds"] <= 1


async def test_extend_requires_reason_and_positive_seconds(butler: dict[str, Any], step: Any) -> None:
    """没有理由或秒数不对：工具报错，不延长、不记审计。"""
    _limits(butler, turn_seconds=5.0, max_turn_seconds=30.0, reply_window_seconds=10.0)
    butler["world"].models.script(
        step(calls=[("c1", "extend_turn", {"seconds": 60, "reason": "  "})]), step(text="好的。"))
    await _say(butler, "慢慢来", "t4")
    assert "reason 必填" in str(butler["world"].models.requests[-1])
    assert await _extensions(butler) == []


async def test_time_hint_appears_only_late_in_the_turn() -> None:
    """用掉六成时限之前没有提示；之后给出剩余秒数并提示先延长；没有时限时不提示。"""
    from types import SimpleNamespace

    from server.engine.butler.limits import time_hint

    loop = asyncio.get_running_loop()
    assert time_hint(SimpleNamespace(deadline=None, started_at=0.0)) is None
    async with asyncio.timeout(100) as deadline:
        early = SimpleNamespace(deadline=deadline, started_at=loop.time())
        assert time_hint(early) is None
        late = SimpleNamespace(deadline=deadline, started_at=loop.time() - 300)
        hint = time_hint(late)
        assert hint is not None and "extend_turn" in hint and "剩余约" in hint

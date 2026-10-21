"""写调用已经发出却没有结果时，不允许模型自动重试。"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from xuanpu_server.runtime.connectors import ConnectorGate


@pytest.mark.parametrize("world", ["local", "docker"], indirect=True)
@pytest.mark.parametrize("fail_audit", [False, True])
async def test_write_timeout_suspends_before_model_can_retry(world: Any, step: Any, read_turn: Any, monkeypatch: Any, fail_audit: bool) -> None:
    """真实 MCP 接收写入后超时，副作用只发生一次，后续脚本模型不被调用。"""
    await world.publish(approval="relaxed")
    original = ConnectorGate.__init__
    def build(self: Any, **kwargs: Any) -> None:
        original(self, **{**kwargs, "timeout_seconds": 0.1})
    monkeypatch.setattr(ConnectorGate, "__init__", build)
    if fail_audit:
        original_gate = world.platform.audit.gate
        failed = False
        async def gate(*args: Any, **kwargs: Any) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("audit database temporarily unavailable")
            await original_gate(*args, **kwargs)
        monkeypatch.setattr(world.platform.audit, "gate", gate)
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(
        step(calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1", "hang": 60})]),
        step(calls=[("c2", "mcp__crm__update_ticket", {"id": "T-1"})]),
        step(text="不该自动重试"),
    )
    events = await asyncio.wait_for(read_turn(await world.platform.sessions.send_message(
        world.member, session.session_id, "修改")), 10)
    assert events[-1].msg.kind == "turn_suspended"
    assert events[-1].msg.data["pending"][0]["reason"] == "tool_outcome_unknown"
    assert len(world.calls()) == 1
    assert len(world.models.requests) == 1

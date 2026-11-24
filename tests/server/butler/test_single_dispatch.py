"""单匠人派工：建派工、启动匠人、立即返回；挂在同一条 trace；预算超限不建记录。"""

from __future__ import annotations

import asyncio
from typing import Any

from server.engine.tracing.models import TraceOwner

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
INSTRUCTION = "INSTR-查询工单 T-1 的进度"


def _dispatch_call(call_id: str = "c2") -> tuple[str, str, dict[str, Any]]:
    """管家派给样例匠人。"""
    return (call_id, "dispatch_artisan",
            {"package": "support-desk", "entry": "triage", "instruction": INSTRUCTION})


async def test_dispatch_returns_without_waiting_for_artisan(
    butler: dict[str, Any], step: Any,
) -> None:
    """匠人卡在半路，管家仍立即回复；派工 running、由会话承载、挂在管家根 span 下。"""
    world = butler["world"]
    hold = asyncio.Event()
    world.models.script_for("INSTR-", step(text="处理中", wait=hold))
    world.models.script(step(calls=[("c1", "list_artisans", {})]),
                        step(calls=[_dispatch_call()]),
                        step(text="已交给工单匠人处理，有结果我再告诉你。"))
    response = await asyncio.wait_for(butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "d1", "text": "帮我看看工单 T-1"}), 30)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["mode"] == "butler"
    [dispatched] = receipt["dispatches"]
    assert dispatched["package"] == "support-desk" and dispatched["state"] == "running"
    assert dispatched["host"] == "session"
    store = world.platform.store
    [record] = await store.dispatch_store.list_for_trace(ALICE, receipt["trace_id"])
    assert record.instruction == INSTRUCTION and record.host_ref is not None
    assert record.artisan.version == "1.0.0" and record.request_id == "d1:d1"
    view = await store.trace_store.get(ALICE, receipt["trace_id"])
    root = next(s for s in view.spans if s.parent_span_id is None)
    assert root.kind == "butler" and record.parent_span_id == root.span_id
    session = await world.platform.sessions.content(butler["alice"], record.host_ref)
    assert (session.package, session.version, session.entry) == ("support-desk", "1.0.0", "triage")
    hold.set()


async def test_budget_blocks_second_artisan_without_record(world: Any, step: Any) -> None:
    """每条 trace 只允许一个匠人时，第二次派工被阻塞且不建记录；管家照常回复。"""
    from .conftest import employee, enable

    enable(world, max_artisans_per_trace=1)
    await world.publish()
    alice = await employee(world)
    world.models.script_for("INSTR-", step(text="好的"), step(text="好的"))
    world.models.script(step(calls=[_dispatch_call("c1")]), step(calls=[_dispatch_call("c2")]),
                        step(text="已派出一位匠人。"))
    from server.engine.butler.service import ButlerMessageBody

    receipt = await world.platform.butler.message(
        alice, ButlerMessageBody(request_id="b1", text="查两遍工单"))
    assert len(receipt["dispatches"]) == 1
    assert len(await world.platform.store.dispatch_store.list_for_trace(
        ALICE, receipt["trace_id"])) == 1
    assert "butler_budget_exceeded" in str(world.models.requests[-1])


async def test_unavailable_artisan_is_refused(butler: dict[str, Any], step: Any) -> None:
    """不存在或未生效的匠人：工具报不可用，不建记录。"""
    world = butler["world"]
    world.models.script(
        step(calls=[("c1", "dispatch_artisan",
                     {"package": "finance", "entry": "pay", "instruction": "付款"})]),
        step(text="企业尚未开通此业务。"))
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": "u1", "text": "帮我付款"})
    receipt = response.json()
    assert receipt["dispatches"] == []
    assert "not_found" in str(world.models.requests[-1])


async def test_delivery_appends_narration_hint(butler: dict[str, Any], step: Any) -> None:
    """匠人收到的说明末尾带说话约定；派工记录里的说明与幂等指纹不变（R7-2）。"""
    from server.core.controlplane.models import AuditQuery
    from server.engine.butler.dispatcher import NARRATE_HINT

    world = butler["world"]
    world.models.script_for("INSTR-", step(text="好的"))
    world.models.script(step(calls=[_dispatch_call()]), step(text="已交给工单匠人。"))
    response = await asyncio.wait_for(butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "h1", "text": "帮我看看工单 T-1"}), 30)
    assert response.status_code == 200, response.text
    store = world.platform.store
    [record] = await store.dispatch_store.list_for_trace(ALICE, response.json()["trace_id"])
    assert record.instruction == INSTRUCTION
    sent = await store.query_audit("acme", AuditQuery(session_id=record.host_ref,
                                                      kind="message_sent", limit=10))
    text = sent[0].data["text"]
    assert text.startswith(INSTRUCTION) and text.endswith(NARRATE_HINT)

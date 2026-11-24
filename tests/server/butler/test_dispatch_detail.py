"""派工细节只读接口（R7-3）：小院「经过」用的任务说明、验收、结果与问答；只给本人。"""

from __future__ import annotations

import asyncio
from typing import Any

from server.engine.tracing.models import TraceOwner

from .conftest import employee, enable

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
WAKE = "artisan_round_finished"


async def _send(butler: dict[str, Any], step: Any, request_id: str, text: str,
                instruction: str) -> str:
    """员工说一句话，管家派一位匠人；返回派工标识。"""
    butler["world"].models.script(
        step(calls=[("c1", "dispatch_artisan", {"package": "support-desk", "entry": "triage",
                                                "instruction": instruction})]),
        step(text="已交给匠人。"))
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": request_id, "text": text})
    assert response.status_code == 200, response.text
    store = butler["world"].platform.store.dispatch_store
    [record] = await store.list_for_trace(ALICE, response.json()["trace_id"])
    return str(record.dispatch_id)


async def _until_delivered(butler: dict[str, Any], dispatch_id: str) -> None:
    """反复对账，直到派工交付。"""
    world = butler["world"]
    async with asyncio.timeout(20):
        while (await world.platform.store.dispatch_store.get(ALICE, dispatch_id)).state \
                != "delivered":
            await world.platform.butler.wakeup.run_once()
            await asyncio.sleep(0.05)


async def test_fresh_dispatch_has_no_checks_or_result(butler: dict[str, Any], step: Any) -> None:
    """刚派出、还没有唤醒事件：说明与承载可读，验收与结果为空，问答为空。"""
    enable(butler["world"], wake_interval_seconds=300)
    hold = asyncio.Event()
    butler["world"].models.script_for("INSTR-F", step(text="处理中", wait=hold))
    dispatch_id = await _send(butler, step, "f1", "看工单", "INSTR-F 看工单 T-1")
    body = (await butler["http"].get(f"/v1/assistant/dispatches/{dispatch_id}")).json()
    assert body["instruction"] == "INSTR-F 看工单 T-1" and body["host"] == "session"
    assert body["artisan"] == {"package": "support-desk", "entry": "triage"}
    assert body["checks"] is None and body["result"] is None and body["items"] == []
    assert "tenant_id" not in body and "user_id" not in body
    hold.set()


async def test_detail_lists_checks_result_and_proxied_answer(
    butler: dict[str, Any], step: Any,
) -> None:
    """交付后：五项验收、截断后的结果、带出处的代答都列出来。"""
    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    world.models.script_for("INSTR-D", step(text="请问客户编号？"))
    world.models.script_for(WAKE, step(calls=[("p1", "answer_artisan", {
        "answer": "C-1024", "source_kind": "conversation", "quote": "客户编号是 C-1024"})]),
        step(text="已代答编号。"))
    dispatch_id = await _send(butler, step, "d1", "帮客户查工单，客户编号是 C-1024",
                              "INSTR-D 查工单")
    world.models.script_for("员工本次原话代答", step(text="查到了。" + "甲" * 900))
    world.models.script_for(WAKE, step(text="已完成。"))
    await _until_delivered(butler, dispatch_id)
    body = (await butler["http"].get(f"/v1/assistant/dispatches/{dispatch_id}")).json()
    assert body["state"] == "delivered"
    assert set(body["checks"]) == {"terminal", "succeeded", "outputs", "no_pending", "no_unknown"}
    assert all(body["checks"].values())
    assert body["result"].startswith("查到了。") and len(body["result"]) == 600
    [proxied] = [i for i in body["items"] if i["kind"] == "proxied"]
    assert proxied["answer"] == "C-1024" and proxied["source_quote"] == "客户编号是 C-1024"


async def test_detail_only_for_owner(butler: dict[str, Any], step: Any) -> None:
    """同租户他人与不存在的 404；机器身份 403。"""
    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    world.models.script_for("INSTR-O", step(text="好的"))
    dispatch_id = await _send(butler, step, "o1", "看工单", "INSTR-O 看工单")
    http = butler["http"]
    butler["as"]["who"] = await employee(world, "usr_bob")
    assert (await http.get(f"/v1/assistant/dispatches/{dispatch_id}")).status_code == 404
    butler["as"]["who"] = world.member
    assert (await http.get(f"/v1/assistant/dispatches/{dispatch_id}")).status_code == 403
    butler["as"]["who"] = butler["alice"]
    assert (await http.get("/v1/assistant/dispatches/nope")).status_code == 404

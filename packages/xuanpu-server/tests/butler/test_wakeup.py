"""唤醒、验收、交付、转问、代答（会话承载的匠人，脚本化模型）。"""

from __future__ import annotations

import asyncio
from typing import Any

from xuanpu_server.tracing.models import TraceOwner

from .conftest import enable

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
WAKE = "artisan_round_finished"


def _dispatch(instruction: str) -> Any:
    """管家派给样例匠人的工具调用。"""
    return ("c1", "dispatch_artisan",
            {"package": "support-desk", "entry": "triage", "instruction": instruction})


async def _send(butler: dict[str, Any], step: Any, request_id: str, text: str, instruction: str,
                ) -> dict[str, Any]:
    """员工说一句话，管家派一位匠人。"""
    butler["world"].models.script(step(calls=[_dispatch(instruction)]), step(text="已交给匠人。"))
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": request_id, "text": text})
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _until(butler: dict[str, Any], predicate: Any, timeout: float = 20.0) -> None:
    """反复对账处理，直到条件成立。"""
    wakeup = butler["world"].platform.butler.wakeup
    async with asyncio.timeout(timeout):
        while not await predicate():
            await wakeup.run_once()
            await asyncio.sleep(0.05)


async def _messages(butler: dict[str, Any]) -> list[dict[str, Any]]:
    """Alice 主对话里管家唤醒后写的消息。"""
    found = await butler["world"].platform.workspace.messages(
        butler["alice"], limit=100, latest=True, private_only=True)
    return [m for m in found if str(m["request_id"]).startswith("butler:")]


async def _dispatch_record(butler: dict[str, Any], trace_id: str) -> Any:
    """这条 trace 的唯一派工。"""
    [record] = await butler["world"].platform.store.dispatch_store.list_for_trace(ALICE, trace_id)
    return record


async def test_artisan_result_is_inspected_and_delivered_once(
    butler: dict[str, Any], step: Any,
) -> None:
    """匠人完成 → 唤醒 → 确定性验收通过 → 交付一次，复核标「辅助判断」；再对账不重复。"""
    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    world.models.script_for("INSTR-A", step(text="工单 T-1 已关闭，处理人王五。"))
    world.models.script_for(WAKE, step(text="结果回答了原请求。"))
    receipt = await _send(butler, step, "w1", "帮我看看工单 T-1", "INSTR-A 查询工单 T-1")

    async def delivered() -> bool:
        return (await _dispatch_record(butler, receipt["trace_id"])).state == "delivered"

    await _until(butler, delivered)
    record = await _dispatch_record(butler, receipt["trace_id"])
    assert record.end_reason == "ok"
    [message] = await _messages(butler)
    assert "工单 T-1 已关闭" in message["text"] and "辅助判断" in message["text"]
    assert message["metadata"]["butler"]["passed"] is True
    for _ in range(3):
        await world.platform.butler.wakeup.run_once()
    assert len(await _messages(butler)) == 1
    view = await world.platform.store.trace_store.get(ALICE, receipt["trace_id"])
    assert sum(1 for s in view.spans if s.kind == "butler" and s.parent_span_id is None) == 2


async def test_failed_round_is_not_rewritten_as_success(butler: dict[str, Any], step: Any) -> None:
    """匠人没有给出任何结果：验收不通过，派工 failed；模型说“很好”也不改结论。"""
    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    world.models.script_for("INSTR-B", step(text=""))
    world.models.script_for(WAKE, step(text="看起来一切顺利。"))
    receipt = await _send(butler, step, "w2", "处理工单 T-2", "INSTR-B 处理工单 T-2")

    async def delivered() -> bool:
        return (await _dispatch_record(butler, receipt["trace_id"])).state == "delivered"

    await _until(butler, delivered)
    record = await _dispatch_record(butler, receipt["trace_id"])
    assert record.end_reason == "check_outputs"
    [message] = await _messages(butler)
    assert "没有通过验收" in message["text"] and "不会自动重跑" in message["text"]
    assert message["metadata"]["butler"]["passed"] is False


async def test_question_forwarded_then_employee_answers(butler: dict[str, Any], step: Any) -> None:
    """匠人问一个员工没说过的事：转问员工；员工答复后原话转给匠人，派工继续并最终交付。"""
    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    world.models.script_for("INSTR-C", step(text="请问客户的联系电话是多少？"))
    world.models.script_for(WAKE, step(calls=[("a1", "ask_user", {"question": "客户的联系电话是多少？"})]),
                            step(text="需要你补充电话。"))
    receipt = await _send(butler, step, "w3", "联系客户", "INSTR-C 联系客户")
    store = world.platform.store.wake_store

    async def asked() -> bool:
        return bool(await store.list_pending(ALICE))

    await _until(butler, asked)
    [item] = await store.list_pending(ALICE)
    assert item.kind == "question" and "电话" in item.question
    assert (await _dispatch_record(butler, receipt["trace_id"])).state == "asking"
    listed = (await butler["http"].get("/v1/assistant/pending")).json()["items"]
    assert [i["pending_id"] for i in listed] == [item.pending_id]
    world.models.script_for(f"〔{item.pending_id}〕", step(text="已登记电话 138，工单完成。"))
    world.models.script_for(WAKE, step(text="已完成。"))
    body = {"request_id": "ans1", "text": "138"}
    answered = await butler["http"].post(f"/v1/assistant/pending/{item.pending_id}/answer", json=body)
    assert answered.status_code == 200, answered.text
    assert answered.json()["state"] == "answered"
    again = await butler["http"].post(f"/v1/assistant/pending/{item.pending_id}/answer", json=body)
    assert again.status_code == 200
    other = await butler["http"].post(f"/v1/assistant/pending/{item.pending_id}/answer",
                                      json={"request_id": "ans2", "text": "139"})
    assert other.status_code == 409

    async def delivered() -> bool:
        return (await _dispatch_record(butler, receipt["trace_id"])).state == "delivered"

    await _until(butler, delivered)
    session = (await _dispatch_record(butler, receipt["trace_id"])).host_ref
    sent = [r for r in await world.platform.store.query_audit("acme", _sent_query(session))]
    assert sum(1 for r in sent if str(r.data["text"]).startswith(f"〔{item.pending_id}〕")) == 1
    assert "已登记电话" in (await _messages(butler))[-1]["text"]


async def test_proxy_answer_uses_literal_fact_with_source(butler: dict[str, Any], step: Any) -> None:
    """匠人问员工说过的客户编号：管家逐字代答并注明出处，不打扰员工。"""
    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    world.models.script_for("INSTR-D", step(text="请问客户编号？"))
    world.models.script_for(WAKE, step(calls=[("p1", "answer_artisan", {
        "answer": "C-1024", "source_kind": "conversation", "quote": "客户编号是 C-1024"})]),
        step(text="已代答编号。"))
    receipt = await _send(butler, step, "w4", "帮客户查工单，客户编号是 C-1024", "INSTR-D 查工单")
    world.models.script_for("员工本次原话代答", step(text="C-1024 的工单已查到：2 条。"))
    world.models.script_for(WAKE, step(text="已完成。"))

    async def delivered() -> bool:
        return (await _dispatch_record(butler, receipt["trace_id"])).state == "delivered"

    await _until(butler, delivered)
    texts = [m["text"] for m in await _messages(butler)]
    assert any("代为答复：C-1024" in t and "出处" in t for t in texts)
    assert any("2 条" in t for t in texts)
    assert await world.platform.store.wake_store.list_pending(ALICE) == ()


async def test_speculative_proxy_becomes_question(butler: dict[str, Any], step: Any) -> None:
    """反例：员工没说过的“事实”（推测）被拒，改为转问员工；问题原样给员工。"""
    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    world.models.script_for("INSTR-E", step(text="请问客户编号？"))
    world.models.script_for(WAKE, step(calls=[("p1", "answer_artisan", {
        "answer": "C-2048", "source_kind": "conversation", "quote": "客户编号大概是 C-2048"})]),
        step(text="我猜是 C-2048。"))
    receipt = await _send(butler, step, "w5", "帮客户查工单", "INSTR-E 查工单")
    store = world.platform.store.wake_store

    async def asked() -> bool:
        return bool(await store.list_pending(ALICE))

    await _until(butler, asked)
    [item] = await store.list_pending(ALICE)
    assert item.kind == "question" and "客户编号" in item.question
    assert (await _dispatch_record(butler, receipt["trace_id"])).state == "asking"
    session = (await _dispatch_record(butler, receipt["trace_id"])).host_ref
    sent = await world.platform.store.query_audit("acme", _sent_query(session))
    assert not any("C-2048" in str(r.data["text"]) for r in sent)


async def test_next_input_answers_pending_or_is_new_request(butler: dict[str, Any], step: Any) -> None:
    """员工下一句话被认出是答复时原话转给匠人；输入始终保存在主对话。"""
    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    world.models.script_for("INSTR-F", step(text="请问需要几份？"))
    world.models.script_for(WAKE, step(calls=[("a1", "ask_user", {"question": "需要几份？"})]),
                            step(text="问员工。"))
    await _send(butler, step, "w6", "打印材料", "INSTR-F 打印")
    store = world.platform.store.wake_store

    async def asked() -> bool:
        return bool(await store.list_pending(ALICE))

    await _until(butler, asked)
    [item] = await store.list_pending(ALICE)
    world.models.script_for(f"〔{item.pending_id}〕", step(text="好的，3 份。"))
    world.models.script(step(calls=[("x1", "answer_pending", {"pending_id": item.pending_id})]),
                        step(text="已转告匠人。"))
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": "w7", "text": "三份就行"})
    assert response.status_code == 200, response.text
    assert response.json()["answered_pending"] == [item.pending_id]
    answered = await store.get_pending(ALICE, item.pending_id)
    assert answered.answer == "三份就行" and answered.answer_source.kind == "employee"
    saved = await world.platform.workspace.find_message(butler["alice"], "w7:user")
    assert saved is not None and saved["text"] == "三份就行"


def _sent_query(session: str | None) -> Any:
    """匠人会话里员工侧发出的消息。"""
    from xuanpu_server.controlplane.models import AuditQuery

    return AuditQuery(session_id=session, kind="message_sent", limit=1000)


async def test_artisan_approval_is_never_proxied(butler: dict[str, Any], step: Any) -> None:
    """匠人要做需审批的写操作：只提示员工去待确认，不代答；员工批准后匠人继续并交付。"""
    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    world.models.script_for("INSTR-G", step(calls=[("u1", "mcp__crm__update_ticket", {"id": "T-9"})]),
                            step(text="工单 T-9 已更新。"))
    world.models.script_for(WAKE, step(text="已完成。"))
    receipt = await _send(butler, step, "w8", "把工单 T-9 关掉", "INSTR-G 关闭工单 T-9")
    store = world.platform.store.wake_store

    async def asked() -> bool:
        return bool(await store.list_pending(ALICE))

    await _until(butler, asked)
    [item] = await store.list_pending(ALICE)
    assert item.kind == "approval" and "待确认" in item.question
    refused = await butler["http"].post(f"/v1/assistant/pending/{item.pending_id}/answer",
                                        json={"request_id": "x", "text": "同意"})
    assert refused.status_code == 409
    record = await _dispatch_record(butler, receipt["trace_id"])
    assert record.state == "asking"
    platform = world.platform
    [pending] = await platform.sessions.pending(butler["alice"], record.host_ref)
    resumed = await platform.sessions.resume(butler["alice"], record.host_ref,
                                             {pending["request_id"]: {"granted": True}})
    async for _ in resumed.stream.events():
        pass

    async def delivered() -> bool:
        return (await _dispatch_record(butler, receipt["trace_id"])).state == "delivered"

    await _until(butler, delivered)
    assert (await store.get_pending(ALICE, item.pending_id)).state == "resolved"
    assert "T-9 已更新" in (await _messages(butler))[-1]["text"]


async def test_review_unavailable_still_delivers_deterministically(
    butler: dict[str, Any], step: Any,
) -> None:
    """管家复核超出轮数预算时不复核，只按确定性结论交付，且不带辅助判断。"""
    enable(butler["world"], wake_interval_seconds=300, max_butler_turns_per_trace=1)
    world = butler["world"]
    world.models.script_for("INSTR-H", step(text="查到 3 条记录。"))
    receipt = await _send(butler, step, "w9", "查记录", "INSTR-H 查记录")

    async def delivered() -> bool:
        return (await _dispatch_record(butler, receipt["trace_id"])).state == "delivered"

    await _until(butler, delivered)
    [message] = await _messages(butler)
    assert "查到 3 条记录" in message["text"] and "辅助判断" not in message["text"]


async def test_cancel_dispatch_follows_real_terminal(butler: dict[str, Any], step: Any) -> None:
    """员工要求停下：管家请求取消，派工以匠人实际停下（cancelled）收尾，不提前宣称已取消。"""
    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    hold = asyncio.Event()
    world.models.script_for("INSTR-I", step(text="处理中", wait=hold))
    receipt = await _send(butler, step, "w10", "跑个长任务", "INSTR-I 长任务")
    record = await _dispatch_record(butler, receipt["trace_id"])
    world.models.script(step(calls=[("k1", "cancel_dispatch", {"dispatch_id": record.dispatch_id})]),
                        step(text="已请求停止。"))
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": "w11", "text": "别做了，停下"})
    assert response.status_code == 200, response.text
    assert (await _dispatch_record(butler, receipt["trace_id"])).state == "running"
    world.models.script_for(WAKE, step(text="已停止。"))

    async def delivered() -> bool:
        return (await _dispatch_record(butler, receipt["trace_id"])).state == "delivered"

    await _until(butler, delivered)
    assert (await _dispatch_record(butler, receipt["trace_id"])).end_reason == "cancelled"
    assert "已停止" in (await _messages(butler))[-1]["text"]


async def test_bad_event_does_not_block_others(butler: dict[str, Any], step: Any) -> None:
    """一条处理失败的事件保持未确认、下一轮重试，不挡住后面的派工交付。"""
    from datetime import UTC, datetime

    from xuanpu_server.butler.wake_models import ButlerEvent

    enable(butler["world"], wake_interval_seconds=300)
    world = butler["world"]
    await world.platform.store.wake_store.emit(ButlerEvent(
        event_id="bev_bad", tenant_id="acme", user_id="usr_alice", dispatch_id="dsp_missing",
        key="round:0", kind="finished", created_at=datetime(2000, 1, 1, tzinfo=UTC)))
    world.models.script_for("INSTR-J", step(text="完成了。"))
    world.models.script_for(WAKE, step(text="好。"))
    receipt = await _send(butler, step, "w12", "做件事", "INSTR-J 做事")

    async def delivered() -> bool:
        return (await _dispatch_record(butler, receipt["trace_id"])).state == "delivered"

    await _until(butler, delivered)
    left = await world.platform.store.wake_store.unacked("acme")
    assert [e.event_id for e in left] == ["bev_bad"]

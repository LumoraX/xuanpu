"""权限不因派工、代答、待处理项扩大（R5 总门槛）：同租户另一名员工看不到、改不了；
代答不能借用别人说过的话。"""

from __future__ import annotations

from typing import Any

from .conftest import employee, enable
from .test_wakeup import ALICE, WAKE, _dispatch_record, _send, _until


async def test_pending_items_are_owner_only(butler: dict[str, Any], step: Any) -> None:
    """Alice 的待处理项：Bob 列不出、答不了（404），答复也不会转给匠人。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for("INSTR-G", step(text="请问发票抬头？"))
    world.models.script_for(WAKE, step(calls=[("a1", "ask_user", {"question": "发票抬头是什么？"})]),
                            step(text="需要你补充抬头。"))
    receipt = await _send(butler, step, "g1", "开发票", "INSTR-G 开发票")
    store = world.platform.store.wake_store

    async def asked() -> bool:
        return bool(await store.list_pending(ALICE))

    await _until(butler, asked)
    [item] = await store.list_pending(ALICE)
    butler["as"]["who"] = await employee(world, "usr_bob")
    assert (await butler["http"].get("/v1/assistant/pending")).json()["items"] == []
    refused = await butler["http"].post(f"/v1/assistant/pending/{item.pending_id}/answer",
                                        json={"request_id": "bob-1", "text": "Bob 公司"})
    assert refused.status_code == 404
    assert (await store.get_pending(ALICE, item.pending_id)).state == "open"
    assert (await _dispatch_record(butler, receipt["trace_id"])).state == "asking"
    for path in (f"/v1/traces/{receipt['trace_id']}",):
        assert (await butler["http"].get(path)).status_code == 404


async def test_proxy_cannot_quote_another_employee(butler: dict[str, Any], step: Any) -> None:
    """Bob 在他自己的对话里说过客户编号；Alice 没说过。管家拿 Bob 的原话代答 Alice 的匠人被拒，
    改为转问 Alice，编号不会发给匠人。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    bob = await employee(world, "usr_bob")
    butler["as"]["who"] = bob
    world.models.script(step(text="好的，记下了。"))
    said = await butler["http"].post("/v1/assistant/messages",
                                     json={"request_id": "bob-said", "text": "客户编号是 C-7777"})
    assert said.status_code == 200, said.text
    butler["as"]["who"] = butler["alice"]
    world.models.script_for("INSTR-Q", step(text="请问客户编号？"))
    world.models.script_for(WAKE, step(calls=[("p1", "answer_artisan", {
        "answer": "C-7777", "source_kind": "conversation", "quote": "客户编号是 C-7777"})]),
        step(text="我替你答了。"))
    receipt = await _send(butler, step, "q1", "帮客户查工单", "INSTR-Q 查工单")
    store = world.platform.store.wake_store

    async def asked() -> bool:
        return bool(await store.list_pending(ALICE))

    await _until(butler, asked)
    [item] = await store.list_pending(ALICE)
    assert item.kind == "question"
    record = await _dispatch_record(butler, receipt["trace_id"])
    assert record.state == "asking"
    sent = await world.platform.store.query_audit("acme", _sent(record.host_ref))
    assert not any("C-7777" in str(r.data.get("text", "")) for r in sent)


def _sent(session: str | None) -> Any:
    """匠人会话里发给匠人的消息。"""
    from server.core.controlplane.models import AuditQuery

    return AuditQuery(session_id=session, kind="message_sent", limit=100)


async def test_model_disabled_keeps_overview_notifications_decisions_and_more(
    butler: dict[str, Any], step: Any, monkeypatch: Any,
) -> None:
    """模型停用（R5 总门槛）：概览、通知、既有的待确认与待处理决定、「更多」用的原接口都可用，
    全程不再请求模型；对管家说话返回 503 且原话保留。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from server.core.errors import RuntimeUnavailableError

    world, http = butler["world"], butler["http"]
    enable(world, wake_interval_seconds=300)
    due = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).replace(microsecond=0)
    choice = {"package": "@reminders", "entry": "create", "reminder": {
        "title": "交周报", "due_at": due.isoformat(), "timezone": "Asia/Shanghai", "repeat": "once"}}
    world.models.script(step(calls=[("c1", "propose", {"choice": choice})]), step(text="请确认。"))
    drafted = await http.post("/v1/assistant/messages", json={"request_id": "md1", "text": "明天提醒我交周报"})
    assert drafted.status_code == 200, drafted.text
    draft = drafted.json()["reminder_draft"]
    world.models.script_for("INSTR-M", step(text="请问客户编号？"))
    world.models.script_for(WAKE, step(calls=[("a1", "ask_user", {"question": "客户编号是多少？"})]),
                            step(text="需要你补充。"))
    await _send(butler, step, "md2", "查工单", "INSTR-M 查工单")
    store = world.platform.store.wake_store

    async def asked() -> bool:
        return bool(await store.list_pending(ALICE))

    await _until(butler, asked)
    [item] = await store.list_pending(ALICE)

    async def down(*_args: Any, **_kwargs: Any) -> Any:
        """取模型客户端失败。"""
        raise RuntimeUnavailableError("模型已停用")

    def no_session(*_args: Any, **_kwargs: Any) -> Any:
        """已建好的客户端也采样失败（管家引擎缓存了客户端）。"""
        raise RuntimeUnavailableError("模型已停用")

    monkeypatch.setattr(world.models, "create", down)
    monkeypatch.setattr(world.models, "session", no_session)
    before = len(world.models.requests)
    overview = await http.get("/v1/dashboard")
    assert overview.status_code == 200 and overview.json()["blocks"]
    assert (await http.get("/v1/notifications")).status_code == 200
    data = {k: v for k, v in draft.items() if k != "request_id"}
    confirmed = await http.post("/v1/assistant/proposals/md1/confirm", json={"data": data})
    assert confirmed.status_code == 200, confirmed.text
    answered = await http.post(f"/v1/assistant/pending/{item.pending_id}/answer",
                               json={"request_id": "md-ans", "text": "C-1"})
    assert answered.status_code == 200 and answered.json()["state"] == "answered"
    reminders = await http.get("/v1/reminders")
    assert reminders.status_code == 200 and [r["title"] for r in reminders.json()["items"]] == ["交周报"]
    assert (await http.get("/v1/workspace/items")).status_code == 200
    failed = await http.post("/v1/assistant/messages", json={"request_id": "md3", "text": "再帮我看看"})
    assert failed.status_code == 503
    texts = [m["text"] for m in await world.platform.workspace.messages(butler["alice"], limit=20)]
    assert "再帮我看看" in texts
    assert len(world.models.requests) == before

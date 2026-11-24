"""概览四块的取数：今天、等我处理、进行中、未读通知；数量来自全部记录，摘要至多 5 条。"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from server.domains.dashboard.service import day_bounds
from server.domains.reminders.models import ReminderInput
from server.engine.tracing.models import SpanFact, TraceOwner

from .conftest import enable_trace, overview


async def test_today_counts_reminders_events_and_due_todos(board: dict[str, Any]) -> None:
    """当天提醒、与今天有交集的日程、今天及之前到期的未完成待办；明天的与已完成的不算。"""
    platform, alice = board["world"].platform, board["alice"]
    now = time.time()
    start, end = day_bounds(now, "UTC")
    due = now + (end - now) / 2
    for i in range(6):
        await platform.reminders.create(alice, ReminderInput(
            title=f"提醒{i}", due_at=due, timezone="UTC"), f"rem-{i}")
    await platform.reminders.create(alice, ReminderInput(
        title="明天的", due_at=end + 3600, timezone="UTC"), "rem-tomorrow")
    ws = platform.workspace
    await ws.create(alice, "event", {"title": "周会", "start_at": now, "end_at": now + 60,
                                     "timezone": "UTC"}, "ev-1")
    await ws.create(alice, "todo", {"title": "逾期", "due_at": start - 10}, "td-1")
    await ws.create(alice, "todo", {"title": "以后", "due_at": end + 10}, "td-2")
    await ws.create(alice, "todo", {"title": "已完成", "due_at": start + 1, "status": "done"}, "td-3")
    body = await overview(board)
    today = body["by_key"]["today"]
    assert today["status"] == "ok" and today["count"] == 8
    assert len(today["items"]) == 5
    kinds = {item["kind"] for item in today["items"]}
    assert "todo" in kinds and "event" in kinds
    overdue = [i for i in today["items"] if i["kind"] == "todo"]
    assert overdue[0]["overdue"] is True and overdue[0]["title"] == "逾期"


async def test_unread_counts_all_and_drops_after_read(board: dict[str, Any]) -> None:
    """未读总数来自存储；读掉一条就少一条。"""
    platform, alice = board["world"].platform, board["alice"]
    fired = []
    for i in range(7):
        record = await platform.reminders.create(alice, ReminderInput(
            title=f"到期{i}", due_at=time.time() + 100, timezone="UTC"), f"fire-{i}")
        fired.append(await platform.reminders.store.fire(record, time.time() + 101))
    unread = (await overview(board))["by_key"]["unread"]
    assert unread["count"] == 7 and len(unread["items"]) == 5
    assert unread["items"][0]["title"] == "到期6"
    await platform.reminders.read(alice, fired[0].notification_id)
    assert (await overview(board))["by_key"]["unread"]["count"] == 6


async def test_waiting_includes_pending_drafts(board: dict[str, Any]) -> None:
    """未处理的助手草稿算作等我处理。"""
    platform, alice = board["world"].platform, board["alice"]
    await platform.workspace.append_message(
        alice, "assistant", "要我帮你记一件待办吗？", "draft-1:assistant",
        metadata={"route": {"workspace_draft": {"kind": "todo", "data": {"title": "周报"}}}})
    waiting = (await overview(board))["by_key"]["waiting"]
    assert waiting["count"] == 1
    assert waiting["items"][0] == {"kind": "proposal", "ref": "draft-1",
                                   "title": "要我帮你记一件待办吗？",
                                   "at": waiting["items"][0]["at"]}


async def test_in_progress_shows_active_traces_only_when_enabled(board: dict[str, Any]) -> None:
    """追踪关闭时这一块不出现；开启后列出活动 trace，并标出在等本人的。"""
    world = board["world"]
    assert "in_progress" not in (await overview(board))["by_key"]
    enable_trace(world, "acme")
    store = world.platform.store.trace_store
    owner = TraceOwner(tenant_id="acme", user_id="usr_alice")
    at = datetime.now(UTC)
    for trace_id, state in (("trc_run", "running"), ("trc_wait", "waiting_input"),
                            ("trc_done", "completed")):
        await store.open_trace(owner, trace_id=trace_id, origin="user", title=trace_id, at=at)
        await store.apply(owner, trace_id, [SpanFact(seq=1, span_id="root", at=at, kind="butler",
                                                     name="管家", state=state)])
    block = (await overview(board))["by_key"]["in_progress"]
    assert block["count"] == 2
    waiting = {item["ref"]: item["waiting_me"] for item in block["items"]}
    assert waiting == {"trc_run": False, "trc_wait": True}

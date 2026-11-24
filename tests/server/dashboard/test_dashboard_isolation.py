"""概览隔离：他人看不到；停用员工与机器身份被拒；关闭的块不出现而不是报错。"""

from __future__ import annotations

import time
from typing import Any

from server.domains.reminders.models import ReminderInput

from .conftest import overview


async def test_other_employee_sees_only_own(board: dict[str, Any]) -> None:
    """Alice 的提醒与通知不进入 Bob 的概览。"""
    platform, alice = board["world"].platform, board["alice"]
    record = await platform.reminders.create(alice, ReminderInput(
        title="Alice 的", due_at=time.time() + 100, timezone="UTC"), "r-1")
    await platform.reminders.store.fire(record, time.time() + 101)
    assert (await overview(board))["by_key"]["unread"]["count"] == 1
    bob = await overview(board, "bob")
    assert all(block["count"] == 0 for block in bob["blocks"])
    assert "Alice" not in str(bob)


async def test_disabled_employee_and_machine_identity_refused(board: dict[str, Any]) -> None:
    """停用员工与不代表员工的 API key 整个请求被拒，不返回任何块。"""
    world, http = board["world"], board["http"]
    board["as"]["who"] = world.member
    assert (await http.get("/v1/dashboard")).status_code == 403
    await world.platform.identity.store.set_user_status(
        "acme", "usr_alice", "disabled", reason="admin", at=time.time())
    board["as"]["who"] = board["alice"]
    assert (await http.get("/v1/dashboard")).status_code == 403


async def test_switched_off_blocks_are_absent(board: dict[str, Any]) -> None:
    """追踪关闭：没有「进行中」；其余三块照常。"""
    body = await overview(board)
    assert [block["key"] for block in body["blocks"]] == ["today", "waiting", "unread"]
    assert all(block["status"] == "ok" for block in body["blocks"])


async def test_bad_timezone_is_request_error(board: dict[str, Any]) -> None:
    """时区名无效是请求错误，不悄悄换成默认时区。"""
    response = await board["http"].get("/v1/dashboard", params={"timezone": "Mars/Base"})
    assert response.status_code == 400

"""给员工看的名字（业务包 displayName、工具 displayName）：会话、业务包列表与待确认审批里给出显示名，
技术名照旧保留；没有声明时回退到技术名。"""
from __future__ import annotations

from typing import Any

import httpx
from xuanpu_server.api.app import create_app


async def test_session_package_and_pending_carry_display_names(world: Any, step: Any, read_turn: Any) -> None:
    """会话与业务包列表带匠人显示名；写操作审批项带工具显示名，target 仍是运行时工具名。"""
    await world.publish()
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with httpx.AsyncClient(transport=transport, base_url="https://office.test",
                                 headers={"Authorization": "Bearer " + world.member_key}) as client:
        created = await client.post("/v1/sessions", json={"package": "support-desk"})
        assert created.status_code == 201, created.text
        session = created.json()
        assert session["display_name"] == "工单助手" and session["package"] == "support-desk"
        listed = (await client.get("/v1/sessions")).json()["sessions"]
        assert listed[0]["display_name"] == "工单助手"
        packages = (await client.get("/v1/packages")).json()["packages"]
        assert packages[0]["display_name"] == "工单助手"
        world.models.script(step(text="需要更新工单", calls=[("c1", "mcp__crm__update_ticket", {"id": "T-1"})]))
        await read_turn(await world.platform.sessions.send_message(world.member, session["session_id"], "更新工单"))
        pending = (await client.get(f"/v1/sessions/{session['session_id']}/pending")).json()["pending"]
        assert pending[0]["detail"]["target"] == "mcp__crm__update_ticket"
        assert pending[0]["detail"]["display_name"] == "修改工单"


async def test_missing_display_name_falls_back_to_technical_name(world: Any) -> None:
    """包与版本不存在时没有显示名（调用方用技术名），不抛错也不编造名字。"""
    assert await world.platform.packages.labels("acme", "no-such", "1.0.0") is None
    await world.publish()
    labels = await world.platform.packages.labels("acme", "support-desk", "1.0.0")
    assert labels is not None and labels.display_name == "工单助手"
    assert labels.tools == {"mcp__crm__update_ticket": "修改工单"}

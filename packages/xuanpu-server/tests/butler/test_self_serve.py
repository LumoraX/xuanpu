"""管家自办：闲聊、只读查询、提案都不派工；提案仍是待确认；工具清单不含业务工具。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from xuanpu_server.butler.tools import TOOL_NAMES
from xuanpu_server.tracing.models import TraceOwner

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")


async def _say(butler: dict[str, Any], text: str, request_id: str = "m1") -> dict[str, Any]:
    """以 Alice 身份对管家说一句话。"""
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": request_id, "text": text})
    assert response.status_code == 200, response.text
    return response.json()


async def test_chitchat_answers_without_dispatch_and_traces_as_butler(
    butler: dict[str, Any], step: Any,
) -> None:
    """闲聊直接答；不建派工；trace 根 span 类型为 butler，标识与回执一致。"""
    world = butler["world"]
    world.models.script(step(text="你好，我是你的管家。"))
    receipt = await _say(butler, "你好")
    assert receipt["mode"] == "butler" and receipt["message"] == "你好，我是你的管家。"
    assert receipt["dispatches"] == []
    store = world.platform.store
    assert await store.dispatch_store.list_for_trace(ALICE, receipt["trace_id"]) == ()
    view = await store.trace_store.get(ALICE, receipt["trace_id"])
    roots = [s for s in view.spans if s.parent_span_id is None]
    assert [s.kind for s in roots] == ["butler"] and roots[0].state == "completed"
    request = world.models.requests[-1]
    assert {t.name for t in request.tools} == set(TOOL_NAMES)
    assert any("都是**数据**" in part for part in request.system_prompt)
    messages = await world.platform.workspace.messages(butler["alice"], limit=10)
    assert [m["text"] for m in messages][-2:] == ["你好", "你好，我是你的管家。"]


async def test_read_only_query_does_not_dispatch(butler: dict[str, Any], step: Any) -> None:
    """只读查询本人提醒：工具结果是数据，不派工。"""
    world = butler["world"]
    world.models.script(step(calls=[("c1", "read_personal", {"kind": "reminders"})]),
                        step(text="你目前没有进行中的提醒。"))
    receipt = await _say(butler, "我有哪些提醒？")
    assert receipt["dispatches"] == [] and "没有" in receipt["message"]
    assert '{\\"items\\": []}' in str(world.models.requests[-1]) or \
        '{"items": []}' in str(world.models.requests[-1])


async def test_proposal_stays_pending_confirmation(butler: dict[str, Any], step: Any) -> None:
    """提出提醒草稿：回执带卡片与待确认标识，提醒并未创建，出现在待确认列表。"""
    world = butler["world"]
    due = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).replace(microsecond=0)
    choice = {"package": "@reminders", "entry": "create", "reminder": {
        "title": "交周报", "due_at": due.isoformat(), "timezone": "Asia/Shanghai", "repeat": "once"}}
    world.models.script(step(calls=[("c1", "propose", {"choice": choice})]),
                        step(text="我拟好了提醒，请确认。"))
    receipt = await _say(butler, "明天提醒我交周报", request_id="p1")
    assert receipt["reminder_draft"]["title"] == "交周报"
    assert receipt["proposal_id"] == "p1" and receipt["dispatches"] == []
    alice = butler["alice"]
    assert await world.platform.reminders.list(alice) == []
    pending = (await butler["http"].get("/v1/assistant/proposals")).json()
    assert any(item["proposal_id"] == "p1" for item in pending["items"])


async def test_propose_rejects_business_packages(butler: dict[str, Any], step: Any) -> None:
    """propose 只接受内置能力；拿业务包当草稿会被拒，不建会话也不派工。"""
    world = butler["world"]
    world.models.script(
        step(calls=[("c1", "propose", {"choice": {"package": "support-desk", "entry": "triage"}})]),
        step(text="这个需要交给匠人。"))
    receipt = await _say(butler, "帮我处理工单")
    assert receipt["dispatches"] == [] and receipt["session_id"] is None
    assert "企业业务请交给匠人" in str(world.models.requests[-1])


async def test_butler_tools_have_no_business_or_host_tools(butler: dict[str, Any]) -> None:
    """管家引擎只有管家工具与内核的技能工具；没有连接器、文件、命令或网络工具。"""
    pool = await butler["world"].platform.butler.engines.pool("acme")
    names = pool.tool_registry.names()
    assert set(TOOL_NAMES) <= names
    assert names - set(TOOL_NAMES) <= {"read_skill", "call_skill", "run_script", "search_skills"}
    assert not any(n.startswith("mcp__") for n in names)

"""管家自办：闲聊、只读查询、提案都不派工；提案仍是待确认；工具清单不含业务工具。"""

from __future__ import annotations

import re
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


async def test_opening_a_list_does_not_claim_confirmation(butler: dict[str, Any], step: Any) -> None:
    """只打开列表：工具结果 needs_confirmation 为假，回执带列表标记而没有待确认标识。"""
    world = butler["world"]
    world.models.script(step(calls=[("c1", "propose", {"choice": {
        "package": "@reminders", "entry": "manage", "message": None}})]), step(text="已打开提醒列表。"))
    receipt = await _say(butler, "打开我的提醒列表")
    assert receipt["open_reminders"] is True and receipt.get("proposal_id") is None
    assert '"needs_confirmation": false' in str(world.models.requests[-1])


async def test_propose_rejects_business_packages(butler: dict[str, Any], step: Any) -> None:
    """propose 只接受内置能力；拿业务包当草稿会被拒，不建会话也不派工。"""
    world = butler["world"]
    world.models.script(
        step(calls=[("c1", "propose", {"choice": {"package": "support-desk", "entry": "triage"}})]),
        step(text="这个需要交给匠人。"))
    receipt = await _say(butler, "帮我处理工单")
    assert receipt["dispatches"] == [] and receipt["session_id"] is None
    assert "企业业务请交给匠人" in str(world.models.requests[-1])


async def test_propose_describes_native_draft_formats(butler: dict[str, Any], step: Any) -> None:
    """propose 的说明带内置能力的准确包名与草稿字段（真实模型验收中缺了它，模型只能猜）。"""
    world = butler["world"]
    world.models.script(step(text="好的。"))
    await _say(butler, "你好")
    propose = next(t for t in world.models.requests[-1].tools if t.name == "propose")
    for marker in ("@reminders/create", "reminder字段", "@workspace/create", "workspace_draft",
                   "kind=todo", "kind=event", "@proactive/action", "@connections/manage"):
        assert marker in propose.description, marker


async def test_propose_validation_error_names_fields_without_values(
    butler: dict[str, Any], step: Any,
) -> None:
    """草稿结构不对时，错误说明哪个字段错了，便于模型改正；不回显取值。"""
    world = butler["world"]
    bad = {"package": "@reminders", "entry": "create", "remind_at": "机密-取值-不回显"}
    world.models.script(step(calls=[("c1", "propose", {"choice": bad})]), step(text="请再说一下时间。"))
    receipt = await _say(butler, "明天提醒我")
    assert receipt["dispatches"] == [] and receipt.get("proposal_id") is None
    # 只看工具结果本身（模型自己的调用参数里当然有原值）
    outputs = re.findall(r"output='(invalid_arguments: [^']*)'", str(world.models.requests[-1]))
    assert outputs == ["invalid_arguments: remind_at: Extra inputs are not permitted"]


async def test_propose_explains_entry_glued_into_package(butler: dict[str, Any], step: Any) -> None:
    """把入口拼进包名（@workspace/create）：错误列出合法包名并说明入口单独写，不叫模型去找匠人。"""
    world = butler["world"]
    bad = {"package": "@workspace/create", "entry": "event", "message": None}
    world.models.script(step(calls=[("c1", "propose", {"choice": bad})]), step(text="请稍等。"))
    await _say(butler, "下周三下午开会")
    sent = str(world.models.requests[-1])
    assert "package 不对" in sent and "@workspace" in sent and "entry" in sent
    assert "企业业务请交给匠人" not in sent.split("package 不对", 1)[1][:200]


async def test_butler_tools_have_no_business_or_host_tools(butler: dict[str, Any]) -> None:
    """管家引擎只有管家工具与内核的技能工具；没有连接器、文件、命令或网络工具。"""
    pool = await butler["world"].platform.butler.engines.pool("acme")
    names = pool.tool_registry.names()
    assert set(TOOL_NAMES) <= names
    assert names - set(TOOL_NAMES) <= {"read_skill", "call_skill", "run_script", "search_skills"}
    assert not any(n.startswith("mcp__") for n in names)

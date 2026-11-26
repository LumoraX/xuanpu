"""管家自办：闲聊、只读查询、提案都不派工；提案仍是待确认；工具清单不含业务工具。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from server.engine.butler.tools import TOOL_NAMES
from server.engine.tracing.models import TraceOwner

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


async def test_propose_people_validation_error_exposes_field_and_reason(
    butler: dict[str, Any], step: Any,
) -> None:
    """@people/record 草稿校验失败时，必须把具体字段位置与原因返回给模型，不能只返 ValueError。"""
    world = butler["world"]
    bad = {
        "package": "@people",
        "entry": "record",
        "message": None,
        "people_draft": {
            "record": {"record_kind": "meeting", "content": "和袁新沟通项目"},
            "orgs": [{"action": "link", "temp_ref": "o1", "profile": {"name": "上海理想"}}],
        },
    }
    world.models.script(step(calls=[("c1", "propose", {"choice": bad})]), step(text="已收到错误，我来修正。"))
    await _say(butler, "记录袁新")
    outputs = re.findall(r"output='(invalid_arguments: [^']*)'", str(world.models.requests[-1]))
    assert len(outputs) == 1
    assert "orgs.0" in outputs[0] and "org_id" in outputs[0]
    assert outputs[0] != "invalid_arguments: ValueError"


async def test_propose_people_normalizes_hoisted_refs_and_blank_dates(
    butler: dict[str, Any], step: Any,
) -> None:
    """顶层误放的 person_refs/org_refs/matter_refs 自动归入 record，空串日期自动转 None。"""
    world = butler["world"]
    draft_choice = {
        "package": "@people",
        "entry": "record",
        "message": None,
        "people_draft": {
            "record": {"record_kind": "meeting", "title": "沟通项目", "content": "袁新沟通壁仞项目"},
            "person_refs": ["p1"],
            "org_refs": ["o1"],
            "matter_refs": ["m1"],
            "orgs": [{"action": "create", "temp_ref": "o1", "profile": {"name": "上海理想信息产业(集团)有限公司"}}],
            "persons": [{"action": "create", "temp_ref": "p1", "profile": {"name": "袁新"}}],
            "matters": [{"action": "create", "temp_ref": "m1", "title": "壁仞项目"}],
            "relations": [{
                "from_kind": "person", "from_ref": "p1",
                "target_kind": "org", "target_ref": "o1",
                "relation_type": "works_at", "valid_from": "", "valid_to": "",
            }],
        },
    }
    world.models.script(
        step(calls=[("c1", "propose", {"choice": draft_choice})]),
        step(text="已整理好草稿，请核对确认。"),
    )
    receipt = await _say(butler, "袁新在理想信息沟通壁仞项目")
    draft = receipt["people_draft"]
    assert draft["record"]["person_refs"] == ["p1"]
    assert draft["record"]["org_refs"] == ["o1"]
    assert draft["record"]["matter_refs"] == ["m1"]
    assert draft["relations"][0]["valid_from"] is None
    assert draft["relations"][0]["valid_to"] is None


async def test_propose_value_error_exposes_reason_not_class_name(
    butler: dict[str, Any], step: Any,
) -> None:
    """业务校验抛出的 ValueError 把可读原因返回给模型，不吞成裸 ValueError。"""
    world = butler["world"]
    bad = {"package": "@people", "entry": "record", "message": "夹带消息", "people_draft": None}
    world.models.script(step(calls=[("c1", "propose", {"choice": bad})]), step(text="好的。"))
    await _say(butler, "记个联系人")
    outputs = re.findall(r"output='(invalid_arguments: [^']*)'", str(world.models.requests[-1]))
    assert outputs == ["invalid_arguments: 人物建议不能夹带其他操作"]


async def test_butler_requests_use_low_reasoning_effort(butler: dict[str, Any], step: Any) -> None:
    """管家默认声明 low 推理强度，避免轻路由与整理被高深度思考拖成几十秒一轮。"""
    world = butler["world"]
    world.models.script(step(text="好的。"))
    await _say(butler, "你好")
    assert world.models.requests[-1].reasoning_effort == "low"


async def test_butler_stops_early_on_consecutive_tool_errors(
    butler: dict[str, Any], step: Any,
) -> None:
    """同一工具连续报错时，第 2 次注入中性提醒，第 4 次熔断停止，不空转耗尽整轮迭代。"""
    world = butler["world"]
    bad_calls = [
        step(calls=[(f"e{i}", "propose", {
            "choice": {"package": "@people", "entry": "record", "message": f"第{i}次错"}
        })])
        for i in range(8)
    ]
    world.models.script(*bad_calls)
    response = await butler["http"].post(
        "/v1/assistant/messages",
        json={"request_id": "req-err-loop", "text": "记个联系人"},
    )
    assert response.status_code == 503
    assert "连续重试同一操作" in response.text
    # 最多跑 4 次模型采样就被熔断，而不是跑到 MAX_ITERATIONS=12
    assert len(world.models.requests) == 4
    assert "has returned an error 2 times in a row" in str(world.models.requests[2])


async def test_incremental_person_update_without_name_succeeds(
    butler: dict[str, Any], step: Any,
) -> None:
    """增量 update 已有人物/公司时未重复传 profile.name，也能自动补齐并保留原资料。"""
    world = butler["world"]
    who = butler["as"]["who"]
    created = await world.platform.people.create_person(
        who,
        {
            "name": "朱佳",
            "organization": "上海电信",
            "contacts": [{"kind": "phone", "value": "18901646624", "label": "手机", "primary": True}],
        },
        "seed-zhujia",
    )
    pid = created["id"]
    draft_choice = {
        "package": "@people",
        "entry": "record",
        "message": None,
        "people_draft": {
            "record": {"record_kind": "note", "content": "朱佳也在上海理想信息产业（集团）有限公司"},
            "persons": [
                {
                    "action": "update",
                    "person_id": pid,
                    "profile": {"organization": "上海理想信息产业（集团）有限公司"},
                }
            ],
        },
    }
    world.models.script(
        step(calls=[("c1", "propose", {"choice": draft_choice})]),
        step(text="已整理好更新草稿。"),
    )
    receipt = await _say(butler, "刚刚的朱佳也是理想信息的")
    updated = receipt["people_draft"]["persons"][0]
    assert updated["profile"]["name"] == "朱佳"
    assert updated["profile"]["organization"] == "上海理想信息产业（集团）有限公司"
    assert updated["profile"]["contacts"][0]["value"] == "18901646624"


async def test_butler_tools_have_no_business_or_host_tools(butler: dict[str, Any]) -> None:
    """管家引擎只有管家工具与内核的技能工具；没有连接器、文件、命令或网络工具。"""
    pool = await butler["world"].platform.butler.engines.pool("acme")
    names = pool.tool_registry.names()
    assert set(TOOL_NAMES) <= names
    assert names - set(TOOL_NAMES) <= {"read_skill", "call_skill", "run_script", "search_skills"}
    assert not any(n.startswith("mcp__") for n in names)

"""私人办公助理自主办理与全域只读/信息整理闭环测试：

- 全域只读：today / todos / events / files / file_content / followups / knowledge；
- 私人事务自主办理：capture_bundle（自动拆解整理入图谱与行动项）、待办新建/完成、日程新建、提醒新建/取消、整理文档存为 Markdown 文件；
- 安全边界：员工关闭 auto_execute_personal 时拒绝直接写入并退回草稿模式；跨员工隔离（Bob 无法查询或撤回 Alice 的自主办理记录）；
- 一键撤回：支持撤回单条新建/修改与复合整理包，幂等重试不报错。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .conftest import employee


async def _say(
    butler: dict[str, Any], text: str, request_id: str, *, input_files: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"request_id": request_id, "text": text}
    if input_files:
        body["input_files"] = input_files
    response = await butler["http"].post("/v1/assistant/messages", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_personal_autonomy_bundle_and_undo(butler: dict[str, Any], step: Any) -> None:
    """自主模式下随手记/会议纪要直接落库图谱与待办，回执带可撤回动作，点击撤回后全量回收。"""
    world, http, alice = butler["world"], butler["http"], butler["alice"]
    due = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).replace(microsecond=0)
    bundle = {
        "record": {
            "record_kind": "meeting",
            "title": "半岛酒店项目沟通",
            "location": "半岛酒店 201",
            "content": "与袁新沟通壁仞项目，明天提交方案。",
            "summary": "明确壁仞项目推进节奏",
            "decisions": ["明天提交方案"],
            "open_questions": ["预算上限待确认"],
            "person_refs": ["p1"],
            "org_refs": ["o1"],
            "matter_refs": ["m1"],
        },
        "orgs": [{"action": "create", "temp_ref": "o1", "profile": {"name": "上海理想信息产业(集团)有限公司"}}],
        "persons": [{
            "action": "create",
            "temp_ref": "p1",
            "profile": {
                "name": "袁新",
                "organization": "上海理想信息产业(集团)有限公司",
                "contacts": [{"kind": "phone", "value": "18017087533", "label": "手机", "primary": True}],
            },
        }],
        "matters": [{"action": "create", "temp_ref": "m1", "title": "壁仞项目"}],
        "todos": [{"title": "明天提交壁仞项目方案", "due_at": due.isoformat(), "matter_ref": "m1"}],
        "reminders": [{"title": "提交壁仞方案", "due_at": due.isoformat(), "timezone": "Asia/Shanghai"}],
        "relations": [
            {"from_kind": "person", "from_ref": "p1", "target_kind": "org", "target_ref": "o1", "relation_type": "works_at"},
            {"from_kind": "person", "from_ref": "p1", "target_kind": "matter", "target_ref": "m1", "relation_type": "matter_role"},
        ],
    }
    world.models.script(
        step(calls=[("a1", "act_personal", {"action": "capture_bundle", "bundle": bundle})]),
        step(text="已为你自动整理归档：新建了公司、袁新名片、壁仞项目及 1 条待办和 1 条提醒。"),
    )
    receipt = await _say(butler, "刚和袁新在半岛酒店201聊了壁仞项目", "auto-bundle-1")
    actions = receipt.get("executed_actions") or []
    assert len(actions) == 1
    assert actions[0]["action"] == "capture_bundle"
    assert actions[0]["undoable"] is True
    assert "袁新" in actions[0]["summary"]

    # 确实已自动落库，无需点确认卡
    assert len(await world.platform.people.list_people(alice, q="袁新")) == 1
    assert len(await world.platform.people.list_orgs(alice, q="上海理想")) == 1
    assert len(await world.platform.workspace.list(alice, kind="todo")) == 1
    assert len(await world.platform.reminders.list(alice, status="active")) == 1

    # Bob 不能查看或撤回 Alice 的自主办理记录
    butler["as"]["who"] = await employee(world, "usr_bob")
    assert (await http.get("/v1/assistant/actions/auto-bundle-1")).status_code == 404
    assert (
        await http.post(f"/v1/assistant/actions/auto-bundle-1/{actions[0]['action_id']}/undo")
    ).status_code == 404

    # Alice 一键撤回（含幂等重放）
    butler["as"]["who"] = alice
    undone = await http.post(f"/v1/assistant/actions/auto-bundle-1/{actions[0]['action_id']}/undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["state"] == "undone"
    assert undone.json()["result"]["undone"] >= 5
    replay = await http.post(f"/v1/assistant/actions/auto-bundle-1/{actions[0]['action_id']}/undo")
    assert replay.status_code == 200 and replay.json()["state"] == "undone"

    status = await http.get("/v1/assistant/actions/auto-bundle-1")
    assert status.status_code == 200 and status.json()["undone"] == [actions[0]["action_id"]]
    assert await world.platform.people.list_people(alice, q="袁新") == []
    assert await world.platform.workspace.list(alice, kind="todo") == []
    assert await world.platform.reminders.list(alice, status="active") == []


async def test_multi_step_personal_actions_read_file_and_save_doc(butler: dict[str, Any], step: Any) -> None:
    """同一轮可连续读上传文本文件、建待办、勾选完成、查今日全景并保存整理好的 Markdown 文档。"""
    world, alice = butler["world"], butler["alice"]
    uploaded = await world.platform.workspace.upload(
        alice, "meeting-notes.md", "# 周会纪要\n- 完成 Q4 报价核对\n".encode(),
        "text/markdown", "upload-1",
    )
    world.models.script(
        step(calls=[("r1", "read_personal", {"kind": "file_content", "item_id": uploaded["id"]})]),
        step(calls=[
            ("a1", "act_personal", {"action": "create_todo", "title": "完成 Q4 报价核对"}),
            ("a2", "act_personal", {
                "action": "save_note_file",
                "name": "Q4周会整理",
                "content": "# Q4 周会整理\n\n已拆解待办：完成 Q4 报价核对\n",
            }),
        ]),
        step(text="已读取会议纪要，新建待办并保存整理文档 Q4周会整理.md。"),
    )
    receipt = await _say(
        butler,
        "帮我整理刚上传的周会纪要",
        "multi-1",
        input_files=[{"item_id": uploaded["id"], "revision": 1}],
    )
    assert len(receipt["executed_actions"]) == 2
    assert "完成 Q4 报价核对" in str(world.models.requests[1])
    todos = await world.platform.workspace.list(alice, kind="todo")
    assert len(todos) == 1 and todos[0]["data"]["status"] == "open"
    files = await world.platform.workspace.list(alice, kind="file")
    assert any(f["data"]["name"] == "Q4周会整理.md" for f in files)

    # 下一轮：先查待办和今日全景，再直接勾选完成并撤回恢复为未完成
    todo_id = todos[0]["id"]
    world.models.script(
        step(calls=[
            ("r2", "read_personal", {"kind": "today"}),
            ("r3", "read_personal", {"kind": "todos", "status": "open"}),
        ]),
        step(calls=[("a3", "act_personal", {"action": "complete_todo", "item_id": todo_id})]),
        step(text="已将「完成 Q4 报价核对」标记为完成。"),
    )
    done_receipt = await _say(butler, "把 Q4 报价核对标成已完成", "multi-2")
    assert (await world.platform.workspace.get(alice, todo_id))["data"]["status"] == "done"

    aid = done_receipt["executed_actions"][0]["action_id"]
    undone = await butler["http"].post(f"/v1/assistant/actions/multi-2/{aid}/undo")
    assert undone.status_code == 200
    assert (await world.platform.workspace.get(alice, todo_id))["data"]["status"] == "open"


async def test_autonomy_disabled_refuses_direct_write(butler: dict[str, Any], step: Any) -> None:
    """员工关闭 auto_execute_personal 后，act_personal 拒绝直接写入并引导退回草稿确认。"""
    world, alice = butler["world"], butler["alice"]
    cur = await world.platform.workspace.settings(alice)
    await world.platform.workspace.update_settings(
        alice, {"auto_execute_personal": False}, cur["version"]
    )
    world.models.script(
        step(calls=[("a1", "act_personal", {"action": "create_todo", "title": "不应直接写入"})]),
        step(text="你已关闭自主办理，我改用草稿卡片请你确认。"),
    )
    receipt = await _say(butler, "记个待办", "no-auto-1")
    assert receipt.get("executed_actions") is None
    assert await world.platform.workspace.list(alice, kind="todo") == []
    # 确认偏好确实注入了管家输入
    req = world.models.requests[0]
    payload = json.loads(
        req.input_items[-1].content
        if isinstance(req.input_items[-1].content, str)
        else req.input_items[-1].content[0].text
    )
    assert payload["personal"]["auto_execute_personal"] is False
    assert "关闭了「私人事务自主办理」" in str(world.models.requests[-1])


async def test_butler_reads_artisan_capabilities_and_recent_dispatches(
    butler: dict[str, Any], step: Any
) -> None:
    """管家通过 list_artisans 读取匠人能力画像（是否收材料/连接器/沙盒），并能查本人最近派工与交付结果。"""
    from datetime import UTC, datetime

    from server.engine.butler.models import ArtisanRef, DispatchRecord, dispatch_fingerprint
    from server.engine.butler.wake_models import ButlerEvent, WakeDecision
    from server.engine.tracing.models import TraceOwner

    world, http, alice = butler["world"], butler["http"], butler["alice"]
    owner = TraceOwner(tenant_id="acme", user_id="usr_alice")
    ref = ArtisanRef(
        package="office-assistant",
        version="1.0.6",
        digest="a" * 64,
        entry="office",
        decision_ref="catalog:office-assistant@1.0.6",
        display_name="办公文档匠人",
    )
    now_dt = datetime.now(UTC)
    rec = DispatchRecord(
        dispatch_id="dsp_hist_1",
        tenant_id="acme",
        user_id="usr_alice",
        request_id="req-hist:d1",
        input_fingerprint=dispatch_fingerprint(
            trace_id="trc_hist_1",
            artisan=ref,
            instruction="整理季度会议纪要",
            materials=("ws_doc_1@1",),
        ),
        trace_id="trc_hist_1",
        artisan=ref,
        host="task",
        host_ref="tsk_hist_1",
        instruction="整理季度会议纪要",
        materials=("ws_doc_1@1",),
        state="delivered",
        created_at=now_dt,
        updated_at=now_dt,
    )
    await world.platform.dispatches.create(owner, rec, max_per_trace=10, max_active_traces=10)
    ev, _ = await world.platform.store.wake_store.emit(
        ButlerEvent(
            event_id="ev_hist_1",
            tenant_id="acme",
            user_id="usr_alice",
            dispatch_id="dsp_hist_1",
            kind="finished",
            key="task:completed:1",
            created_at=now_dt,
        )
    )
    await world.platform.store.wake_store.decide(
        "acme",
        ev.event_id,
        WakeDecision(
            action="deliver",
            checks={"terminal": True, "succeeded": True, "outputs": True, "no_pending": True, "no_unknown": True},
            artisan_text="已提取 3 条核心决议与 4 项行动待办。",
        ),
    )

    world.models.script(
        step(calls=[
            ("c1", "list_artisans", {}),
            ("c2", "read_personal", {"kind": "dispatches", "query": "会议纪要"}),
            ("c3", "check_dispatch", {"dispatch_id": "dsp_hist_1"}),
        ]),
        step(text="已查到匠人列表以及上次办公文档匠人的交付成果。"),
    )
    receipt = await _say(butler, "看看有哪些匠人，顺便查一下上次会议纪要派工的结果", "artisan-read-1")
    assert "交付成果" in receipt["message"]
    last_req = str(world.models.requests[-1])
    assert "accepts_materials" in last_req
    assert "connectors" in last_req
    assert "dsp_hist_1" in last_req
    assert "已提取 3 条核心决议与 4 项行动待办。" in last_req

    # HTTP /v1/assistant/artisans 也同步透出能力画像
    api_artisans = await http.get("/v1/assistant/artisans")
    assert api_artisans.status_code == 200
    for item in api_artisans.json()["items"]:
        assert "accepts_materials" in item and "connectors" in item and "capabilities" in item

    # Bob 查不到 Alice 的最近派工
    butler["as"]["who"] = await employee(world, "usr_bob")
    world.models.script(
        step(calls=[("c4", "read_personal", {"kind": "dispatches"})]),
        step(text="你目前没有派工记录。"),
    )
    await _say(butler, "查我最近的派工", "bob-disp-1")
    assert "dsp_hist_1" not in str(world.models.requests[-1])
    butler["as"]["who"] = alice

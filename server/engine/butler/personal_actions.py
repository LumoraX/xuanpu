"""私人助理全域只读、本人私域自主办理与一键撤回引擎。

- 所有操作严格走既有领域权威服务（workspace / reminders / people / proactive / knowledge），
  按 ``(tenant_id, user_id)`` 强隔离并保留幂等与平台审计。
- 只允许操作员工本人私域数据；绝不触碰对外发送、组织审批、交接、共享或企业外部连接器。
- 员工可在个人设置 ``auto_execute_personal`` 中随时关闭自主办理，关闭后自动退回草稿确认模式。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.ids import new_id
from server.domains.people.graph_rules import merge_org_profile_patch
from server.domains.people.models import PeopleBundleInput
from server.domains.people.routing import hydrate_bundle_updates
from server.domains.people.rules import merge_profile_patch
from server.domains.proactive.models import ActionInput
from server.domains.reminders.models import ReminderInput
from server.engine.butler.personal_guardrails import (
    assert_wake_personal_action_allowed,
    detect_event_conflicts,
    merge_personal_people,
    trash_personal_item,
    undo_bundle_profile_updates,
    undo_people_unmerge,
)
from server.engine.butler.personal_reads import (
    parse_timestamp as parse_timestamp,
)
from server.engine.butler.personal_reads import (
    read_extended_personal as read_extended_personal,
)

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.engine.butler.turn import ButlerTurn
    from server.platform import Platform

logger = logging.getLogger(__name__)
# 单次写入/编辑最终文件体积上限与个人工作台单文件上限（20 MiB）保持一致，支撑 14MB+ / 数十万字长文分批写作与精编
_MAX_NOTE_BYTES = 20 * 1024 * 1024


def _normalize_bundle_timestamps(bundle: dict[str, Any], timezone: str) -> dict[str, Any]:
    """容错：把模型传入的 ISO 时间字符串规范为 UNIX 秒。"""
    copy = dict(bundle)
    rec = copy.get("record")
    if isinstance(rec, dict):
        rec = dict(rec)
        for field in ("occurred_at", "end_at"):
            if field in rec and rec[field] is not None:
                rec[field] = parse_timestamp(rec[field], timezone)
        copy["record"] = rec
    if isinstance(copy.get("todos"), list):
        todos = []
        for t in copy["todos"]:
            if isinstance(t, dict) and t.get("due_at") is not None:
                t = {**t, "due_at": parse_timestamp(t["due_at"], timezone)}
            todos.append(t)
        copy["todos"] = todos
    if isinstance(copy.get("events"), list):
        events = []
        for e in copy["events"]:
            if isinstance(e, dict):
                e = dict(e)
                if e.get("start_at") is not None:
                    e["start_at"] = parse_timestamp(e["start_at"], timezone)
                if e.get("end_at") is not None:
                    e["end_at"] = parse_timestamp(e["end_at"], timezone)
            events.append(e)
        copy["events"] = events
    if isinstance(copy.get("reminders"), list):
        rems = []
        for r in copy["reminders"]:
            if isinstance(r, dict) and r.get("due_at") is not None:
                r = {**r, "due_at": parse_timestamp(r["due_at"], timezone)}
            rems.append(r)
        copy["reminders"] = rems
    return copy


async def execute_personal_action(
    platform: Platform, turn: ButlerTurn, args: dict[str, Any], *, now: float
) -> dict[str, Any]:
    """执行一项本人私域自主办理动作，并把可撤回回执记入 ``turn.executed_actions``。"""
    who = turn.who
    settings = await platform.workspace.settings(who)
    if not bool(settings.get("auto_execute_personal", True)):
        raise InvalidRequestError(
            "员工当前关闭了「私人事务自主办理」，请改用 propose 生成待确认草稿交由员工确认"
        )
    action = str(args.get("action") or "").strip()
    assert_wake_personal_action_allowed(turn, action)
    req_id = turn.action_request_id()
    action_id = new_id("act")
    tz = str(args.get("timezone") or turn.timezone or "Asia/Shanghai")

    if action == "capture_bundle":
        raw_bundle = args.get("bundle")
        if not isinstance(raw_bundle, dict):
            raise InvalidRequestError("capture_bundle 需要提供 bundle 对象")
        normalized = _normalize_bundle_timestamps(raw_bundle, tz)
        hydrated = await hydrate_bundle_updates(platform, who, normalized)
        validated = PeopleBundleInput.model_validate(hydrated).model_dump()
        # 对更新已有公司/人物自动安全合并既有字段并补全版本，同时留存修改前快照供一键真撤回
        prev_orgs: list[dict[str, Any]] = []
        prev_persons: list[dict[str, Any]] = []
        for org in validated.get("orgs", []):
            if org["action"] == "update" and org.get("org_id"):
                existing_org = (await platform.people.get_org(who, org["org_id"]))["org"]
                prev_orgs.append({"org_id": org["org_id"], "profile": dict(existing_org["profile"])})
                org["profile"] = merge_org_profile_patch(existing_org["profile"], org["profile"])
                if org.get("expected_version") is None:
                    org["expected_version"] = existing_org["version"]
        for person in validated.get("persons", []):
            if person["action"] == "update" and person.get("person_id"):
                existing_p = (await platform.people.get_person(who, person["person_id"]))["person"]
                prev_persons.append({"person_id": person["person_id"], "profile": dict(existing_p["profile"])})
                person["profile"] = merge_profile_patch(existing_p["profile"], person["profile"])
                if person.get("expected_version") is None:
                    person["expected_version"] = existing_p["version"]
        result = await platform.people.apply_bundle(who, validated, req_id)
        created_org_ids = [
            o["id"]
            for spec, o in zip(validated.get("orgs", []), result.get("orgs", []), strict=False)
            if spec.get("action") == "create"
        ]
        created_person_ids = [
            p["id"]
            for spec, p in zip(validated.get("persons", []), result.get("persons", []), strict=False)
            if spec.get("action") == "create"
        ]
        created_matter_ids = [
            m["id"]
            for spec, m in zip(validated.get("matters", []), result.get("matters", []), strict=False)
            if spec.get("action") == "create"
        ]
        created_todos = [t["id"] for t in result.get("todos", [])]
        created_events = [e["id"] for e in result.get("events", [])]
        created_reminders = [r["reminder_id"] for r in result.get("reminders", [])]
        note_id = result["record"]["id"] if result.get("record") else None

        summary_parts: list[str] = []
        if result.get("orgs"):
            summary_parts.append("公司：" + "、".join(o["name"] for o in result["orgs"][:4]))
        if result.get("persons"):
            summary_parts.append("人物：" + "、".join(p["name"] for p in result["persons"][:4]))
        if result.get("matters"):
            summary_parts.append("事项：" + "、".join(m["data"].get("title", "") for m in result["matters"][:4]))
        if created_todos:
            summary_parts.append(f"{len(created_todos)} 条待办")
        if created_events:
            summary_parts.append(f"{len(created_events)} 条日程")
        if created_reminders:
            summary_parts.append(f"{len(created_reminders)} 条提醒")
        if not summary_parts and result.get("record"):
            rec = result["record"]
            summary_parts.append("原始记录：" + (rec.get("title") or rec["content"][:24]))
        summary = "；".join(summary_parts) or "个人知识图谱整理"
        record_entry = {
            "action_id": action_id,
            "action": action,
            "title": "已自动整理归档",
            "summary": summary,
            "undoable": bool(
                note_id or created_org_ids or created_person_ids or created_matter_ids
                or created_todos or created_events or created_reminders
                or prev_orgs or prev_persons
            ),
            "undo": {
                "kind": "bundle",
                "note_id": note_id,
                "org_ids": created_org_ids,
                "person_ids": created_person_ids,
                "prev_orgs": prev_orgs,
                "prev_persons": prev_persons,
                "matter_ids": created_matter_ids,
                "todo_ids": created_todos,
                "event_ids": created_events,
                "reminder_ids": created_reminders,
            },
        }
        turn.executed_actions.append(record_entry)
        return {"ok": True, "action_id": action_id, "summary": summary, "result": result}

    if action == "create_todo":
        title = _require_text(args.get("title"), "title")
        data: dict[str, Any] = {
            "title": title,
            "description": str(args.get("description") or ""),
            "status": "open",
            "due_at": parse_timestamp(args.get("due_at"), tz),
            "matter_id": _opt_text(args.get("matter_id")),
        }
        saved = await platform.workspace.create(who, "todo", data, req_id)
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已新建待办",
            "summary": title,
            "undoable": True,
            "undo": {"kind": "workspace_trash", "item_id": saved["id"]},
        }
        turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, "item": saved}

    if action in ("update_todo", "complete_todo", "reopen_todo"):
        item_id = _require_text(args.get("item_id"), "item_id")
        current = await platform.workspace.get(who, item_id)
        if current["kind"] != "todo" or current.get("trashed_at") is not None:
            raise NotFoundError("待办不存在或已回收")
        prev_data = dict(current["data"])
        patch: dict[str, Any] = {}
        if action == "complete_todo":
            patch["status"] = "done"
        elif action == "reopen_todo":
            patch["status"] = "open"
        else:
            if args.get("title") is not None:
                patch["title"] = _require_text(args.get("title"), "title")
            if args.get("description") is not None:
                patch["description"] = str(args.get("description") or "")
            if args.get("status") in ("open", "done"):
                patch["status"] = args["status"]
            if "due_at" in args:
                patch["due_at"] = parse_timestamp(args.get("due_at"), tz)
            if "matter_id" in args:
                patch["matter_id"] = _opt_text(args.get("matter_id"))
        if not patch:
            raise InvalidRequestError("未提供要更新的待办字段")
        updated = await platform.workspace.update(who, item_id, patch, current["version"])
        label = "已完成待办" if patch.get("status") == "done" else "已重开待办" if action == "reopen_todo" else "已更新待办"
        entry = {
            "action_id": action_id,
            "action": action,
            "title": label,
            "summary": str(updated["data"].get("title") or ""),
            "undoable": True,
            "undo": {"kind": "workspace_restore_data", "item_id": item_id, "prev_data": prev_data},
        }
        turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, "item": updated}

    if action == "create_event":
        title = _require_text(args.get("title"), "title")
        start_at = parse_timestamp(args.get("start_at"), tz)
        end_at = parse_timestamp(args.get("end_at"), tz)
        if start_at is None or end_at is None:
            raise InvalidRequestError("create_event 需要提供 start_at 与 end_at")
        data = {
            "title": title,
            "description": str(args.get("description") or ""),
            "start_at": start_at,
            "end_at": end_at,
            "timezone": tz,
            "matter_id": _opt_text(args.get("matter_id")),
        }
        saved = await platform.workspace.create(who, "event", data, req_id)
        schedule_check = await detect_event_conflicts(
            platform, who, start_at=start_at, end_at=end_at, exclude_id=saved["id"],
        )
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已安排日程",
            "summary": title,
            "undoable": True,
            "undo": {"kind": "workspace_trash", "item_id": saved["id"]},
        }
        turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, "item": saved, **schedule_check}

    if action == "update_event":
        item_id = _require_text(args.get("item_id"), "item_id")
        current = await platform.workspace.get(who, item_id)
        if current["kind"] != "event" or current.get("trashed_at") is not None:
            raise NotFoundError("日程不存在或已回收")
        prev_data = dict(current["data"])
        patch = {}
        if args.get("title") is not None:
            patch["title"] = _require_text(args.get("title"), "title")
        if args.get("description") is not None:
            patch["description"] = str(args.get("description") or "")
        if "start_at" in args and args["start_at"] is not None:
            patch["start_at"] = parse_timestamp(args["start_at"], tz)
        if "end_at" in args and args["end_at"] is not None:
            patch["end_at"] = parse_timestamp(args["end_at"], tz)
        if args.get("timezone"):
            patch["timezone"] = tz
        if "matter_id" in args:
            patch["matter_id"] = _opt_text(args.get("matter_id"))
        if not patch:
            raise InvalidRequestError("未提供要更新的日程字段")
        updated = await platform.workspace.update(who, item_id, patch, current["version"])
        schedule_check = await detect_event_conflicts(
            platform,
            who,
            start_at=float(updated["data"]["start_at"]),
            end_at=float(updated["data"]["end_at"]),
            exclude_id=item_id,
        )
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已更新日程",
            "summary": str(updated["data"].get("title") or ""),
            "undoable": True,
            "undo": {"kind": "workspace_restore_data", "item_id": item_id, "prev_data": prev_data},
        }
        turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, "item": updated, **schedule_check}

    if action == "cancel_event":
        item_id = _require_text(args.get("item_id"), "item_id")
        return await trash_personal_item(
            platform, turn, action=action, action_id=action_id,
            expected_kind="event", item_id=item_id,
            not_found_msg="日程不存在或已回收", title_label="已取消日程",
        )

    if action == "delete_todo":
        item_id = _require_text(args.get("item_id"), "item_id")
        return await trash_personal_item(
            platform, turn, action=action, action_id=action_id,
            expected_kind="todo", item_id=item_id,
            not_found_msg="待办不存在或已回收", title_label="已移出待办",
        )

    if action == "merge_people":
        source_id = _require_text(args.get("source_id"), "source_id")
        target_id = _require_text(args.get("target_id"), "target_id")
        return await merge_personal_people(
            platform, turn, action_id=action_id, req_id=req_id,
            source_id=source_id, target_id=target_id,
        )

    if action == "create_reminder":
        title = _require_text(args.get("title"), "title")
        due_at = parse_timestamp(args.get("due_at"), tz)
        if due_at is None:
            raise InvalidRequestError("create_reminder 需要提供 due_at")
        repeat = str(args.get("repeat") or "once")
        body = ReminderInput(title=title, due_at=due_at, timezone=tz, repeat=repeat)  # type: ignore[arg-type]
        saved_rem = await platform.reminders.create(who, body, req_id)
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已设置提醒",
            "summary": saved_rem.title,
            "undoable": True,
            "undo": {"kind": "reminder_cancel", "reminder_id": saved_rem.reminder_id},
        }
        turn.executed_actions.append(entry)
        return {
            "ok": True,
            "action_id": action_id,
            "reminder": {
                "reminder_id": saved_rem.reminder_id,
                "version": saved_rem.version,
                "title": saved_rem.title,
                "due_at": saved_rem.due_at,
                "timezone": saved_rem.timezone,
                "repeat": saved_rem.repeat,
            },
        }

    if action == "cancel_reminder":
        rem_id = _require_text(args.get("reminder_id"), "reminder_id")
        existing = next(
            (r for r in await platform.reminders.list(who, status="active") if r.reminder_id == rem_id),
            None,
        )
        if existing is None:
            raise NotFoundError("活动提醒不存在")
        cancelled_rem = await platform.reminders.cancel(who, rem_id, existing.version)
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已取消提醒",
            "summary": cancelled_rem.title,
            "undoable": False,
            "undo": None,
        }
        turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, "reminder_id": cancelled_rem.reminder_id, "status": cancelled_rem.status}

    if action == "create_matter":
        title = _require_text(args.get("title"), "title")
        data = {
            "title": title,
            "description": str(args.get("description") or ""),
            "status": str(args.get("status") or "active"),
        }
        saved = await platform.workspace.create(who, "matter", data, req_id)
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已新建事项",
            "summary": title,
            "undoable": True,
            "undo": {"kind": "workspace_trash", "item_id": saved["id"]},
        }
        turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, "item": saved}

    if action == "update_matter":
        item_id = _require_text(args.get("item_id"), "item_id")
        current = await platform.workspace.get(who, item_id)
        if current["kind"] != "matter" or current.get("trashed_at") is not None:
            raise NotFoundError("事项不存在或已回收")
        prev_data = dict(current["data"])
        patch = {}
        if args.get("title") is not None:
            patch["title"] = _require_text(args.get("title"), "title")
        if args.get("description") is not None:
            patch["description"] = str(args.get("description") or "")
        if args.get("status") in ("active", "completed", "archived"):
            patch["status"] = args["status"]
        if not patch:
            raise InvalidRequestError("未提供要更新的事项字段")
        updated = await platform.workspace.update(who, item_id, patch, current["version"])
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已更新事项",
            "summary": str(updated["data"].get("title") or ""),
            "undoable": True,
            "undo": {"kind": "workspace_restore_data", "item_id": item_id, "prev_data": prev_data},
        }
        turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, "item": updated}

    if action == "act_followup":
        followup_id = _require_text(args.get("followup_id") or args.get("item_id"), "followup_id")
        followup_action = _require_text(args.get("followup_action"), "followup_action")
        existing_fu = await platform.proactive.get(who, followup_id)
        due_at = parse_timestamp(args.get("due_at"), tz)
        body_fu = ActionInput(
            request_id=req_id,
            version=int(existing_fu["version"]),
            item_version=existing_fu.get("item_version"),
            action=followup_action,  # type: ignore[arg-type]
            due_at=due_at,
            timezone=tz,
        )
        updated_fu = await platform.proactive.act(who, followup_id, body_fu)
        labels = {
            "complete": "已完成跟进",
            "postpone": "已延后跟进",
            "ignore_stage": "已忽略跟进",
            "set_reminder": "已为跟进设提醒",
            "progress": "已记录跟进进展",
        }
        entry = {
            "action_id": action_id,
            "action": action,
            "title": labels.get(followup_action, "已处理跟进"),
            "summary": str(existing_fu.get("title") or followup_id),
            "undoable": False,
            "undo": None,
        }
        turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, "followup": updated_fu}

    if action == "save_note_file":
        raw_name = _require_text(args.get("name") or args.get("title"), "name")
        safe_name = raw_name.replace("/", "-").replace("\\", "-").strip()
        if not safe_name.lower().endswith((".md", ".txt", ".json", ".csv")):
            safe_name += ".md"
        text = str(args.get("content") or "")
        if not text.strip():
            raise InvalidRequestError("save_note_file 的 content 不能为空")
        encoded = text.encode("utf-8")
        if len(encoded) > min(_MAX_NOTE_BYTES, platform.workspace.max_file_bytes):
            raise InvalidRequestError("文档正文超过单文件 20 MiB 上限")
        mime = "text/markdown"
        if safe_name.lower().endswith(".txt"):
            mime = "text/plain"
        elif safe_name.lower().endswith(".json"):
            mime = "application/json"
        elif safe_name.lower().endswith(".csv"):
            mime = "text/csv"
        saved = await platform.workspace.upload(
            who,
            safe_name,
            encoded,
            mime,
            req_id,
            parent_id=_opt_text(args.get("parent_id")),
            matter_id=_opt_text(args.get("matter_id")),
        )
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已保存整理文档",
            "summary": safe_name,
            "undoable": True,
            "undo": {"kind": "workspace_trash", "item_id": saved["id"]},
        }
        turn.executed_actions.append(entry)
        from server.engine.butler.multimodal import validate_mermaid_blocks

        mermaid_warnings = validate_mermaid_blocks(text)
        out_note: dict[str, Any] = {"ok": True, "action_id": action_id, "file": saved}
        if mermaid_warnings:
            out_note["mermaid_warnings"] = mermaid_warnings
        return out_note

    if action == "append_personal_file":
        item_id = _require_text(args.get("item_id"), "item_id")
        chunk_text = str(args.get("content") or "")
        if not chunk_text.strip():
            raise InvalidRequestError("append_personal_file 的 content 不能为空")
        sep = "\n\n" if args.get("separator") is None else str(args.get("separator"))
        item = await platform.workspace.get(who, item_id)
        if item["kind"] != "file" or item.get("trashed_at") is not None:
            raise NotFoundError("目标个人文件不存在或已回收")
        expected_version = args.get("expected_version")
        if expected_version is not None and int(expected_version) != int(item["version"]):
            raise ConflictError("文件版本已变化，请先重新读取最新状态")
        raw_bytes, mime, filename = await platform.workspace.download(who, item_id)
        if b"\x00" in raw_bytes:
            raise InvalidRequestError("append_personal_file 仅支持 UTF-8 文本文件")
        try:
            original = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidRequestError("append_personal_file 仅支持 UTF-8 文本文件") from exc
        combined = (original.rstrip("\n") + sep + chunk_text) if original else chunk_text
        encoded = combined.encode("utf-8")
        if len(encoded) > min(_MAX_NOTE_BYTES, platform.workspace.max_file_bytes):
            raise InvalidRequestError("追加后文档体积超过 20 MiB 上限")
        prev_revision = int(item["data"]["current_revision"])
        saved = await platform.workspace.replace_content(
            who, item_id, encoded, mime, int(item["version"]), req_id,
        )
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已分批追加长文",
            "summary": f"{filename}（+{len(chunk_text)} 字，总计 {len(combined)} 字）",
            "undoable": True,
            "undo": {"kind": "workspace_restore_revision", "item_id": item_id, "revision": prev_revision},
        }
        turn.executed_actions.append(entry)
        return {
            "ok": True,
            "action_id": action_id,
            "file": saved,
            "appended_chars": len(chunk_text),
            "total_chars": len(combined),
            "total_lines": combined.count("\n") + 1,
        }

    if action == "patch_personal_file":
        from server.domains.workspace.patch import (
            TextPatchEdit,
            apply_exact_edits,
            replace_line_range,
        )

        item_id = _require_text(args.get("item_id"), "item_id")
        raw_edits = args.get("edits")
        start_line = args.get("start_line") if type(args.get("start_line")) is int else None
        end_line = args.get("end_line") if type(args.get("end_line")) is int else None
        has_range = start_line is not None and end_line is not None
        if not has_range and (not isinstance(raw_edits, list) or not raw_edits):
            raise InvalidRequestError("patch_personal_file 需要提供 edits 列表或 start_line/end_line 行区间")
        item = await platform.workspace.get(who, item_id)
        if item["kind"] != "file" or item.get("trashed_at") is not None:
            raise NotFoundError("目标个人文件不存在或已回收")
        expected_version = args.get("expected_version")
        if expected_version is not None and int(expected_version) != int(item["version"]):
            raise ConflictError("文件版本已变化，请先重新读取最新正文")
        raw_bytes, mime, filename = await platform.workspace.download(who, item_id)
        try:
            original = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidRequestError("patch_personal_file 仅支持 UTF-8 文本文件") from exc
        if has_range:
            assert start_line is not None and end_line is not None
            patched = replace_line_range(
                original,
                start_line=start_line,
                end_line=end_line,
                replacement=str(args.get("content") or ""),
                filename=filename,
            )
        else:
            edits = [TextPatchEdit.model_validate(e) for e in raw_edits]  # type: ignore[union-attr]
            patched = apply_exact_edits(original, edits, filename=filename)
        encoded = patched.new_text.encode("utf-8")
        if len(encoded) > min(_MAX_NOTE_BYTES, platform.workspace.max_file_bytes):
            raise InvalidRequestError("编辑后文档体积超过 20 MiB 上限")
        prev_revision = int(item["data"]["current_revision"])
        saved = await platform.workspace.replace_content(
            who, item_id, encoded, mime, int(item["version"]), req_id,
        )
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已局部编辑文档",
            "summary": f"{filename}（{patched.applied_count} 处修改）",
            "diff": patched.diff,
            "undoable": True,
            "undo": {
                "kind": "workspace_restore_revision",
                "item_id": item_id,
                "revision": prev_revision,
            },
        }
        turn.executed_actions.append(entry)
        return {
            "ok": True,
            "action_id": action_id,
            "file": saved,
            "diff": patched.diff,
            "applied_count": patched.applied_count,
        }

    if action == "classify_file":
        from server.domains.workspace.taxonomy import (
            CustomTaxonomyRule,
            auto_file_workspace_document,
        )

        item_id = _require_text(args.get("item_id"), "item_id")
        apply_flag = True if args.get("apply") is None else bool(args.get("apply"))
        index_kb = True if args.get("index_knowledge") is None else bool(args.get("index_knowledge"))
        rule_rows = await platform.plugins.collections.list(who, "doc-taxonomy", "rules", limit=100)
        custom_rules = [CustomTaxonomyRule.model_validate(r.data) for r in rule_rows]
        res = await auto_file_workspace_document(
            platform, who, item_id, apply=apply_flag, index_knowledge=index_kb,
            custom_rules=custom_rules, request_id=req_id,
        )
        if apply_flag:
            cls_info = res["classification"]
            entry = {
                "action_id": action_id,
                "action": action,
                "title": "已自动业务归类归档",
                "summary": f"{res['filename']} → {cls_info['folder_name']}（{cls_info['category_name']}）",
                "undoable": True,
                "undo": {"kind": "workspace_restore_data", "item_id": item_id, "prev_data": res["prev_data"]},
            }
            turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, **{k: v for k, v in res.items() if k != "prev_data"}}

    raise InvalidRequestError(f"不支持的私人办理动作：{action}")


async def undo_executed_action(
    platform: Platform, who: Principal, record: dict[str, Any]
) -> dict[str, Any]:
    """按持久化的撤回句柄安全逆转一次本人自主办理动作。"""
    undo = record.get("undo")
    if not record.get("undoable") or not isinstance(undo, dict):
        raise InvalidRequestError("该动作不支持一键撤回")
    kind = str(undo.get("kind") or "")
    undone = 0
    skipped = 0

    async def _trash_workspace(item_id: str) -> None:
        nonlocal undone, skipped
        try:
            item = await platform.workspace.get(who, item_id)
            if item.get("trashed_at") is None:
                await platform.workspace.trash(who, item_id, item["version"])
                undone += 1
            else:
                skipped += 1
        except (NotFoundError, ConflictError):
            skipped += 1

    async def _cancel_reminder(reminder_id: str) -> None:
        nonlocal undone, skipped
        try:
            rem = next(
                (r for r in await platform.reminders.list(who, status="active") if r.reminder_id == reminder_id),
                None,
            )
            if rem is not None:
                await platform.reminders.cancel(who, reminder_id, rem.version)
                undone += 1
            else:
                skipped += 1
        except (NotFoundError, ConflictError):
            skipped += 1

    if kind == "workspace_trash":
        await _trash_workspace(str(undo["item_id"]))
    elif kind == "workspace_restore_trash":
        item_id = str(undo["item_id"])
        try:
            item = await platform.workspace.get(who, item_id)
            if item.get("trashed_at") is not None:
                await platform.workspace.restore(who, item_id, int(item["version"]))
                undone += 1
            else:
                skipped += 1
        except (NotFoundError, ConflictError):
            skipped += 1
    elif kind == "workspace_restore_data":
        item_id = str(undo["item_id"])
        prev_data = undo.get("prev_data")
        if not isinstance(prev_data, dict):
            raise InvalidRequestError("缺少恢复前快照")
        item = await platform.workspace.get(who, item_id)
        if item.get("trashed_at") is not None:
            raise ConflictError("该记录已移入回收站，无法撤回修改")
        await platform.workspace.update(who, item_id, prev_data, item["version"])
        undone += 1
    elif kind == "workspace_restore_revision":
        item_id = str(undo["item_id"])
        prev_rev = int(undo["revision"])
        item = await platform.workspace.get(who, item_id)
        if item.get("trashed_at") is not None:
            raise ConflictError("该文件已移入回收站，无法撤回编辑")
        prev_bytes, mime, _name = await platform.workspace.download(who, item_id, revision=prev_rev)
        await platform.workspace.replace_content(
            who, item_id, prev_bytes, mime, int(item["version"]), new_id("undo"),
        )
        undone += 1
    elif kind == "people_unmerge":
        u, s = await undo_people_unmerge(platform, who, undo)
        undone += u
        skipped += s
    elif kind == "reminder_cancel":
        await _cancel_reminder(str(undo["reminder_id"]))
    elif kind == "bundle":
        note_id = undo.get("note_id")
        if isinstance(note_id, str) and note_id:
            try:
                notes = await platform.people.list_notes(who, limit=100)
                target = next((n for n in notes if n["id"] == note_id), None)
                if target is not None:
                    await platform.people.delete_note(who, note_id, target["version"])
                    undone += 1
                else:
                    skipped += 1
            except (NotFoundError, ConflictError):
                skipped += 1
        for pid in undo.get("person_ids") or []:
            try:
                p = (await platform.people.get_person(who, str(pid)))["person"]
                if p.get("trashed_at") is None:
                    await platform.people.trash_person(who, str(pid), p["version"])
                    undone += 1
            except (NotFoundError, ConflictError):
                skipped += 1
        for oid in undo.get("org_ids") or []:
            try:
                o = (await platform.people.get_org(who, str(oid)))["org"]
                if o.get("trashed_at") is None:
                    await platform.people.trash_org(who, str(oid), o["version"])
                    undone += 1
            except (NotFoundError, ConflictError):
                skipped += 1
        for tid in undo.get("todo_ids") or []:
            await _trash_workspace(str(tid))
        for eid in undo.get("event_ids") or []:
            await _trash_workspace(str(eid))
        for mid in undo.get("matter_ids") or []:
            await _trash_workspace(str(mid))
        for rid in undo.get("reminder_ids") or []:
            await _cancel_reminder(str(rid))
        u, s = await undo_bundle_profile_updates(platform, who, undo)
        undone += u
        skipped += s
    else:
        raise InvalidRequestError("未知撤回类型")

    return {"undone": undone, "skipped": skipped}


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"{name} 不能为空")
    return value.strip()


def _opt_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None

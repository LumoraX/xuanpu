"""私人助理全域只读、本人私域自主办理与一键撤回引擎。

- 所有操作严格走既有领域权威服务（workspace / reminders / people / proactive / knowledge），
  按 ``(tenant_id, user_id)`` 强隔离并保留幂等与平台审计。
- 只允许操作员工本人私域数据；绝不触碰对外发送、组织审批、交接、共享或企业外部连接器。
- 员工可在个人设置 ``auto_execute_personal`` 中随时关闭自主办理，关闭后自动退回草稿确认模式。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.ids import new_id
from server.domains.dashboard.service import day_bounds
from server.domains.knowledge.service import SearchInput
from server.domains.people.graph_rules import merge_org_profile_patch
from server.domains.people.models import PeopleBundleInput
from server.domains.people.routing import hydrate_bundle_updates
from server.domains.people.rules import merge_profile_patch
from server.domains.proactive.models import ActionInput
from server.domains.reminders.models import ReminderInput

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.engine.butler.turn import ButlerTurn
    from server.platform import Platform

logger = logging.getLogger(__name__)
_LIST_MAX = 25
_FILE_PREVIEW_CHARS = 12_000
_MAX_NOTE_CHARS = 200_000


def parse_timestamp(value: Any, timezone: str) -> float | None:
    """兼容 UNIX 秒时间戳与 ISO-8601 字符串，统一转为 UTC 秒。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("时间格式无效")
    if isinstance(value, int | float):
        stamp = float(value)
        if not math.isfinite(stamp) or stamp < 0 or stamp > 253402300799:
            raise ValueError("时间戳超出范围")
        return stamp
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("时间须为 UNIX 秒或 ISO-8601 格式") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(timezone))
        return dt.timestamp()
    raise ValueError("时间格式无效")


async def read_extended_personal(
    platform: Platform, turn: ButlerTurn, kind: str, args: dict[str, Any], *, now: float
) -> dict[str, Any] | None:
    """处理扩展的个人只读域（today/todos/events/files/file_content/knowledge/followups）。"""
    who = turn.who
    q_raw = args.get("query")
    search = q_raw.strip() if isinstance(q_raw, str) and q_raw.strip() else None
    matter_raw = args.get("matter_id")
    matter_id = matter_raw.strip() if isinstance(matter_raw, str) and matter_raw.strip() else None
    limit_raw = args.get("limit")
    limit = limit_raw if type(limit_raw) is int and 1 <= limit_raw <= 50 else _LIST_MAX

    if kind == "today":
        return await _read_today(platform, who, turn.timezone, now=now)
    if kind == "todos":
        status = args.get("status")
        items = await platform.workspace.list(who, kind="todo", search=search)
        out: list[dict[str, Any]] = []
        for item in items:
            data = item["data"]
            if status in ("open", "done") and data.get("status") != status:
                continue
            if matter_id and data.get("matter_id") != matter_id:
                continue
            out.append({
                "id": item["id"],
                "version": item["version"],
                "title": data.get("title", ""),
                "status": data.get("status", "open"),
                "due_at": data.get("due_at"),
                "matter_id": data.get("matter_id"),
                "description": str(data.get("description") or "")[:300],
            })
            if len(out) >= limit:
                break
        return {"items": out}
    if kind == "events":
        items = await platform.workspace.list(who, kind="event", search=search)
        out = []
        for item in items:
            data = item["data"]
            if matter_id and data.get("matter_id") != matter_id:
                continue
            out.append({
                "id": item["id"],
                "version": item["version"],
                "title": data.get("title", ""),
                "start_at": data.get("start_at"),
                "end_at": data.get("end_at"),
                "timezone": data.get("timezone"),
                "matter_id": data.get("matter_id"),
                "description": str(data.get("description") or "")[:300],
            })
            if len(out) >= limit:
                break
        return {"items": out}
    if kind == "files":
        files = await platform.workspace.list(who, kind="file", search=search)
        folders = await platform.workspace.list(who, kind="folder", search=search)
        file_items = [
            {
                "id": f["id"],
                "version": f["version"],
                "name": f["data"].get("name", ""),
                "mime_type": f["data"].get("mime_type"),
                "size": f["data"].get("size", 0),
                "current_revision": f["data"].get("current_revision", 0),
                "matter_id": f["data"].get("matter_id"),
                "parent_id": f["data"].get("parent_id"),
                "source_url": f["data"].get("source_url"),
            }
            for f in files
            if not matter_id or f["data"].get("matter_id") == matter_id
        ][:limit]
        folder_items = [
            {"id": d["id"], "name": d["data"].get("name", ""), "parent_id": d["data"].get("parent_id")}
            for d in folders[:limit]
        ]
        return {"files": file_items, "folders": folder_items}
    if kind == "file_content":
        item_id = args.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise InvalidRequestError("file_content 需要提供 item_id")
        rev_raw = args.get("revision")
        revision = rev_raw if type(rev_raw) is int and rev_raw >= 1 else None
        raw, mime, name = await platform.workspace.download(who, item_id.strip(), revision)
        if b"\x00" in raw:
            raise InvalidRequestError(f"文件「{name}」为二进制格式（{mime}），暂不支持直接文本读取")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidRequestError(f"文件「{name}」不是 UTF-8 文本，无法直接读取正文") from exc
        truncated = len(text) > _FILE_PREVIEW_CHARS
        return {
            "item_id": item_id.strip(),
            "name": name,
            "mime_type": mime,
            "size": len(raw),
            "truncated": truncated,
            "content": text[:_FILE_PREVIEW_CHARS],
        }
    if kind == "knowledge":
        return await _read_knowledge(platform, who, search=search, limit=min(limit, 10))
    if kind == "dispatches":
        records = await platform.dispatches.list_recent(turn.owner(), limit=limit)
        items = []
        needle = search.casefold() if search else None
        for r in records:
            if needle and needle not in (
                f"{r.artisan.package} {r.artisan.entry} {r.artisan.display_name or ''} {r.instruction}"
            ).casefold():
                continue
            items.append({
                "dispatch_id": r.dispatch_id,
                "trace_id": r.trace_id,
                "group_id": r.group_id,
                "package": r.artisan.package,
                "entry": r.artisan.entry,
                "name": r.artisan.display_name or r.artisan.package,
                "host": r.host,
                "state": r.state,
                "end_reason": r.end_reason,
                "instruction": r.instruction[:300],
                "materials": list(r.materials),
                "created_at": r.created_at.isoformat(),
                "updated_at": r.updated_at.isoformat(),
            })
        return {"items": items}
    if kind == "followups":
        rows = await platform.proactive.list(who)
        items = [
            {
                "id": r["id"],
                "version": r["version"],
                "title": r.get("title", ""),
                "status": r.get("status", ""),
                "reason": str(r.get("reason") or "")[:300],
                "due_at": r.get("due_at"),
                "suggested_action": r.get("suggested_action"),
            }
            for r in rows[:limit]
        ]
        return {"items": items}
    return None


async def _read_today(
    platform: Platform, who: Principal, timezone: str, *, now: float
) -> dict[str, Any]:
    """聚合本人今日日程、今日/逾期待办、今日提醒与活跃跟进全景。"""
    start, end = day_bounds(now, timezone)
    reminders = [
        {
            "reminder_id": r.reminder_id,
            "version": r.version,
            "title": r.title,
            "due_at": r.due_at,
            "repeat": r.repeat,
            "overdue": r.due_at < now,
        }
        for r in await platform.reminders.list(who, status="active")
        if r.due_at < end
    ]
    events = []
    for item in await platform.workspace.list(who, kind="event"):
        data = item["data"]
        if data["start_at"] < end and data["end_at"] > start:
            events.append({
                "id": item["id"],
                "version": item["version"],
                "title": data["title"],
                "start_at": data["start_at"],
                "end_at": data["end_at"],
                "matter_id": data.get("matter_id"),
            })
    todos_today: list[dict[str, Any]] = []
    todos_other: list[dict[str, Any]] = []
    for item in await platform.workspace.list(who, kind="todo"):
        data = item["data"]
        if data.get("status") != "open":
            continue
        due = data.get("due_at")
        entry = {
            "id": item["id"],
            "version": item["version"],
            "title": data["title"],
            "due_at": due,
            "overdue": bool(due is not None and due < start),
            "matter_id": data.get("matter_id"),
        }
        if due is not None and due < end:
            todos_today.append(entry)
        elif len(todos_other) < 10:
            todos_other.append(entry)
    followups = [
        {
            "id": r["id"],
            "version": r["version"],
            "title": r.get("title", ""),
            "status": r.get("status", ""),
            "due_at": r.get("due_at"),
        }
        for r in await platform.proactive.list(who)
        if r.get("status") not in ("completed", "dismissed", "ignored")
    ][:10]
    return {
        "timezone": timezone,
        "day_start": start,
        "day_end": end,
        "events": sorted(events, key=lambda x: x["start_at"]),
        "reminders": sorted(reminders, key=lambda x: x["due_at"]),
        "todos_due_today_or_overdue": sorted(todos_today, key=lambda x: x["due_at"] or 0.0),
        "other_open_todos": todos_other,
        "open_followups": followups,
    }


async def _read_knowledge(
    platform: Platform, who: Principal, *, search: str | None, limit: int
) -> dict[str, Any]:
    """检索本人知识库片段与已确认工作记忆（租户未开通时友好降级）。"""
    tenant = who.require_tenant()
    if not platform.knowledge.config.allows(tenant):
        return {"enabled": False, "hits": [], "memories": []}
    hits: list[Any] = []
    if search:
        found = await platform.knowledge.search(who, SearchInput(query=search, limit=limit))
        raw_hits = found.get("items")
        hits = list(raw_hits) if isinstance(raw_hits, list) else []
    mem_body = await platform.work_memory.listing(who)
    raw_mems = mem_body.get("items")
    memories = [
        m for m in (raw_mems if isinstance(raw_mems, list) else [])
        if isinstance(m, dict) and m.get("status") == "confirmed"
    ][:limit]
    return {"enabled": True, "hits": hits, "memories": memories}


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
        # 对更新已有公司/人物自动安全合并既有字段并补全版本，防止覆盖丢失原信息
        for org in validated.get("orgs", []):
            if org["action"] == "update" and org.get("org_id"):
                existing_org = (await platform.people.get_org(who, org["org_id"]))["org"]
                org["profile"] = merge_org_profile_patch(existing_org["profile"], org["profile"])
                if org.get("expected_version") is None:
                    org["expected_version"] = existing_org["version"]
        for person in validated.get("persons", []):
            if person["action"] == "update" and person.get("person_id"):
                existing_p = (await platform.people.get_person(who, person["person_id"]))["person"]
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
            ),
            "undo": {
                "kind": "bundle",
                "note_id": note_id,
                "org_ids": created_org_ids,
                "person_ids": created_person_ids,
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
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已安排日程",
            "summary": title,
            "undoable": True,
            "undo": {"kind": "workspace_trash", "item_id": saved["id"]},
        }
        turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, "item": saved}

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
        entry = {
            "action_id": action_id,
            "action": action,
            "title": "已更新日程",
            "summary": str(updated["data"].get("title") or ""),
            "undoable": True,
            "undo": {"kind": "workspace_restore_data", "item_id": item_id, "prev_data": prev_data},
        }
        turn.executed_actions.append(entry)
        return {"ok": True, "action_id": action_id, "item": updated}

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
        if len(text) > _MAX_NOTE_CHARS:
            raise InvalidRequestError("文档正文超过上限")
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
            text.encode("utf-8"),
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
        return {"ok": True, "action_id": action_id, "file": saved}

    if action == "patch_personal_file":
        from server.domains.workspace.patch import TextPatchEdit, apply_exact_edits

        item_id = _require_text(args.get("item_id"), "item_id")
        raw_edits = args.get("edits")
        if not isinstance(raw_edits, list) or not raw_edits:
            raise InvalidRequestError("patch_personal_file 需要非空 edits 列表")
        edits = [TextPatchEdit.model_validate(e) for e in raw_edits]
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
        patched = apply_exact_edits(original, edits, filename=filename)
        if len(patched.new_text) > _MAX_NOTE_CHARS:
            raise InvalidRequestError("编辑后文档正文超过上限")
        prev_revision = int(item["data"]["current_revision"])
        saved = await platform.workspace.replace_content(
            who, item_id, patched.new_text.encode("utf-8"), mime, int(item["version"]), req_id,
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

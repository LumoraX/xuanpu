"""私人助理全域只读查询引擎（今日全景、待办、日程、文件、知识库、混合记忆、派工与跟进）。

- 所有查询按 ``(tenant_id, user_id)`` 强隔离，经由既有领域权威服务只读返回；
- 独立拆分自 ``personal_actions.py`` 以保持模块职责单一且符合单文件行数规范。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from server.core.errors import InvalidRequestError
from server.domains.dashboard.service import day_bounds
from server.domains.knowledge.service import SearchInput

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.engine.butler.turn import ButlerTurn
    from server.platform import Platform

_LIST_MAX = 25
_FILE_PREVIEW_CHARS = 12_000


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
    """处理扩展的个人只读域（today/todos/events/files/file_content/knowledge/hybrid_recall/dispatches/followups）。"""
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
    if kind == "hybrid_recall":
        from server.domains.people.hybrid_recall import hybrid_recall_personal

        if not search:
            raise InvalidRequestError("hybrid_recall 需要提供 query 关键词或模糊描述")
        hits = await hybrid_recall_personal(platform, who, search, now=now, limit=min(limit, 15))
        return {"query": search, "items": hits}
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

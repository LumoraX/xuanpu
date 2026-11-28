"""管家私域自主办理护栏：唤醒轮白名单收窄、日程冲突检测与可撤回纠错动作（ADR 0024 / 0025）。

- 被匠人回调唤醒的复核轮可能携带外部不可信文本，为防二阶提示注入，
  ``act_personal`` 仅允许纯追加类安全写操作，禁止覆盖、完成或删除已有个人记录；
- 创建/修改日程时由代码确定性计算时间重叠（``conflicts``）与 60 分钟通勤/衔接缓冲
  （``adjacent_events``），绝不依赖模型口算；
- 纠错类动作（``cancel_event`` / ``delete_todo``）走软回收站，支持一键 ``/undo`` 恢复。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.ids import new_id
from server.domains.people.models import MergeInput, UnmergeInput

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.engine.butler.turn import ButlerTurn
    from server.platform import Platform

# 被唤醒复核轮（turn.wake != None）下允许调用的纯追加类私人动作白名单
WAKE_ALLOWED_PERSONAL_ACTIONS = frozenset({
    "save_note_file",
    "capture_bundle",
    "create_todo",
    "create_event",
    "create_reminder",
    "create_matter",
})

ADJACENT_BUFFER_SECONDS = 3600.0


def assert_wake_personal_action_allowed(turn: ButlerTurn, action: str) -> None:
    """被唤醒轮次拦截破坏性/覆盖类私人动作，阻断二阶提示注入。"""
    if turn.wake is None:
        return
    if action not in WAKE_ALLOWED_PERSONAL_ACTIONS:
        raise InvalidRequestError(
            f"被匠人回调唤醒时禁止执行覆盖或删除类私人操作（{action}），请向员工说明后由员工发起确认"
        )


async def detect_event_conflicts(
    platform: Platform,
    who: Principal,
    *,
    start_at: float,
    end_at: float,
    exclude_id: str | None = None,
    buffer_seconds: float = ADJACENT_BUFFER_SECONDS,
) -> dict[str, list[dict[str, Any]]]:
    """确定性扫描本人有效日程，返回时间冲突与 60 分钟内紧邻日程列表。"""
    conflicts: list[dict[str, Any]] = []
    adjacent: list[dict[str, Any]] = []
    events = await platform.workspace.list(who, kind="event")
    for item in events:
        if exclude_id is not None and item["id"] == exclude_id:
            continue
        data = item["data"]
        ev_start = float(data["start_at"])
        ev_end = float(data["end_at"])
        summary = {
            "id": item["id"],
            "title": str(data.get("title") or ""),
            "start_at": ev_start,
            "end_at": ev_end,
            "matter_id": data.get("matter_id"),
        }
        if ev_start < end_at and ev_end > start_at:
            conflicts.append(summary)
            continue
        # 计算非重叠日程的前后间隔，识别 60 分钟内的紧邻排期提醒通勤/转场缓冲
        if ev_end <= start_at and (start_at - ev_end) <= buffer_seconds:
            adjacent.append({
                **summary,
                "relation": "before",
                "gap_minutes": round((start_at - ev_end) / 60.0, 1),
            })
        elif ev_start >= end_at and (ev_start - end_at) <= buffer_seconds:
            adjacent.append({
                **summary,
                "relation": "after",
                "gap_minutes": round((ev_start - end_at) / 60.0, 1),
            })
    conflicts.sort(key=lambda x: float(x["start_at"]))
    adjacent.sort(key=lambda x: float(x["start_at"]))
    return {"conflicts": conflicts, "adjacent_events": adjacent}


async def trash_personal_item(
    platform: Platform,
    turn: ButlerTurn,
    *,
    action: str,
    action_id: str,
    expected_kind: str,
    item_id: str,
    not_found_msg: str,
    title_label: str,
) -> dict[str, Any]:
    """将本人指定待办或日程软移入回收站，并登记可一键恢复的撤回回执。"""
    who = turn.who
    current = await platform.workspace.get(who, item_id)
    if current["kind"] != expected_kind or current.get("trashed_at") is not None:
        raise NotFoundError(not_found_msg)
    trashed = await platform.workspace.trash(who, item_id, int(current["version"]))
    entry = {
        "action_id": action_id,
        "action": action,
        "title": title_label,
        "summary": str(current["data"].get("title") or item_id),
        "undoable": True,
        "undo": {"kind": "workspace_restore_trash", "item_id": item_id},
    }
    turn.executed_actions.append(entry)
    return {"ok": True, "action_id": action_id, "item": trashed}


async def merge_personal_people(
    platform: Platform,
    turn: ButlerTurn,
    *,
    action_id: str,
    req_id: str,
    source_id: str,
    target_id: str,
) -> dict[str, Any]:
    """将本人名下两位重复人物软合并，并登记可一键 ``/undo`` 撤销的回执。"""
    who = turn.who
    src = (await platform.people.get_person(who, source_id))["person"]
    tgt = (await platform.people.get_person(who, target_id))["person"]
    body = MergeInput(
        request_id=req_id,
        target_id=target_id,
        source_version=int(src["version"]),
        target_version=int(tgt["version"]),
    )
    merged = await platform.people.merge_people(who, source_id, body)
    entry = {
        "action_id": action_id,
        "action": "merge_people",
        "title": "已合并重复人物档案",
        "summary": f"{src.get('name', source_id)} → {tgt.get('name', target_id)}",
        "undoable": True,
        "undo": {
            "kind": "people_unmerge",
            "source_id": source_id,
            "target_id": target_id,
        },
    }
    turn.executed_actions.append(entry)
    return {"ok": True, "action_id": action_id, "result": merged}


async def undo_people_unmerge(
    platform: Platform, who: Principal, undo: dict[str, Any],
) -> tuple[int, int]:
    """逆转一次 ``merge_people`` 软合并，返回 ``(undone, skipped)``。"""
    source_id = str(undo["source_id"])
    target_id = str(undo["target_id"])
    try:
        src = (await platform.people.get_person(who, source_id))["person"]
        tgt = (await platform.people.get_person(who, target_id))["person"]
        await platform.people.unmerge_people(
            who,
            source_id,
            UnmergeInput(
                request_id=new_id("unm"),
                source_version=int(src["version"]),
                target_version=int(tgt["version"]),
            ),
        )
        return 1, 0
    except (NotFoundError, ConflictError):
        return 0, 1


def build_note_file_details(saved: dict[str, Any], name: str, mime: str, size: int, text: str) -> dict[str, Any]:
    """为 ``save_note_file`` 构造成果展示卡片的文档预览与元数据明细。"""
    return {
        "item_id": saved["id"],
        "name": name,
        "mime_type": mime,
        "size": size,
        "matter_id": saved["data"].get("matter_id"),
        "preview": text[:1600],
    }


def build_classify_details(item_id: str, res: dict[str, Any]) -> dict[str, Any]:
    """为 ``classify_file`` 构造归类目录、标签与知识库状态明细。"""
    cls_info = res["classification"]
    return {
        "item_id": item_id,
        "name": res["filename"],
        "category_name": cls_info["category_name"],
        "folder_name": cls_info["folder_name"],
        "tags": list(cls_info.get("suggested_tags") or ()),
        "knowledge_indexed": bool(res.get("knowledge_indexed")),
        "matter_id": res["file"]["data"].get("matter_id"),
    }


def build_bundle_outcome_details(result: dict[str, Any]) -> dict[str, Any]:
    """为 ``capture_bundle`` 构造前端一次性成果展示所需的精简结构化明细。"""
    rec = result.get("record")
    record_view: dict[str, Any] | None = None
    if isinstance(rec, dict):
        record_view = {
            "id": rec.get("id"),
            "record_kind": rec.get("record_kind") or "note",
            "title": str(rec.get("title") or ""),
            "location": str(rec.get("location") or ""),
            "occurred_at": rec.get("occurred_at"),
            "summary": str(rec.get("summary") or "")[:600],
            "decisions": [str(x) for x in (rec.get("decisions") or ())[:12]],
            "open_questions": [str(x) for x in (rec.get("open_questions") or ())[:12]],
            "attachment_file_ids": [str(x) for x in (rec.get("attachment_file_ids") or ())[:10]],
        }
    return {
        "record": record_view,
        "orgs": [{"id": o["id"], "name": o["name"]} for o in (result.get("orgs") or ())[:12]],
        "persons": [
            {"id": p["id"], "name": p["name"], "organization": str(p.get("organization") or "")}
            for p in (result.get("persons") or ())[:16]
        ],
        "matters": [
            {"id": m["id"], "title": str(m.get("data", {}).get("title") or "")}
            for m in (result.get("matters") or ())[:8]
        ],
        "todos": [
            {
                "id": t["id"],
                "title": str(t.get("data", {}).get("title") or ""),
                "due_at": t.get("data", {}).get("due_at"),
                "status": str(t.get("data", {}).get("status") or "open"),
            }
            for t in (result.get("todos") or ())[:16]
        ],
        "events": [
            {
                "id": e["id"],
                "title": str(e.get("data", {}).get("title") or ""),
                "start_at": e.get("data", {}).get("start_at"),
                "end_at": e.get("data", {}).get("end_at"),
            }
            for e in (result.get("events") or ())[:10]
        ],
        "reminders": [
            {
                "id": r.get("reminder_id"),
                "title": str(r.get("title") or ""),
                "due_at": r.get("due_at"),
            }
            for r in (result.get("reminders") or ())[:10]
        ],
        "relations_count": len(result.get("relations") or ()),
    }


def attach_turn_materials_to_bundle(
    normalized: dict[str, Any], turn: ButlerTurn,
) -> dict[str, Any]:
    """将本轮员工附带的 ``materials`` 自动补齐到 ``record.attachment_file_ids``，避免模型漏传导致原件断链。"""
    if not turn.materials:
        return normalized
    rec = normalized.get("record")
    if not isinstance(rec, dict):
        return normalized
    existing = [str(fid).strip() for fid in (rec.get("attachment_file_ids") or ()) if str(fid).strip()]
    merged = list(dict.fromkeys([*existing, *(m.item_id for m in turn.materials)]))[:10]
    return {**normalized, "record": {**rec, "attachment_file_ids": merged}}


async def bind_bundle_attachment_matters(
    platform: Platform, who: Principal, result: dict[str, Any],
) -> list[dict[str, Any]]:
    """若 ``capture_bundle`` 落库了原始记录且关联了事项，则将未绑定事项的附件文件同步绑定，并返回撤回快照。"""
    rec = result.get("record")
    if not isinstance(rec, dict):
        return []
    file_ids = [str(fid) for fid in (rec.get("attachment_file_ids") or ()) if str(fid).strip()]
    matter_ids = rec.get("matter_ids")
    matter_id = rec.get("matter_id") or (matter_ids[0] if isinstance(matter_ids, list) and matter_ids else None)
    if not file_ids or not isinstance(matter_id, str) or not matter_id:
        return []
    prev_files: list[dict[str, Any]] = []
    for fid in file_ids:
        try:
            item = await platform.workspace.get(who, fid)
            if item["kind"] != "file" or item.get("trashed_at") is not None:
                continue
            if item["data"].get("matter_id") is not None:
                continue
            prev_data = {k: v for k, v in item["data"].items() if k in ("name", "parent_id", "matter_id", "tags")}
            await platform.workspace.update(who, fid, {"matter_id": matter_id}, int(item["version"]))
            prev_files.append({"item_id": fid, "prev_data": prev_data})
        except (NotFoundError, ConflictError):
            continue
    return prev_files


async def undo_bundle_profile_updates(
    platform: Platform, who: Principal, undo: dict[str, Any],
) -> tuple[int, int]:
    """还原 ``capture_bundle`` 对既有公司/人物档案及附件元数据的合并更新快照，补齐完整真撤回。"""
    undone = 0
    skipped = 0
    for snap in undo.get("prev_orgs") or []:
        try:
            oid = str(snap["org_id"])
            cur = (await platform.people.get_org(who, oid))["org"]
            await platform.people.update_org(who, oid, dict(snap["profile"]), int(cur["version"]))
            undone += 1
        except (NotFoundError, ConflictError, KeyError):
            skipped += 1
    for snap in undo.get("prev_persons") or []:
        try:
            pid = str(snap["person_id"])
            cur = (await platform.people.get_person(who, pid))["person"]
            await platform.people.update_person(who, pid, dict(snap["profile"]), int(cur["version"]))
            undone += 1
        except (NotFoundError, ConflictError, KeyError):
            skipped += 1
    for snap in undo.get("prev_files") or []:
        try:
            fid = str(snap["item_id"])
            cur_f = await platform.workspace.get(who, fid)
            if cur_f.get("trashed_at") is None:
                await platform.workspace.update(who, fid, dict(snap["prev_data"]), int(cur_f["version"]))
                undone += 1
            else:
                skipped += 1
        except (NotFoundError, ConflictError, KeyError):
            skipped += 1
    return undone, skipped

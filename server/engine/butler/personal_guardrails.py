"""管家私域自主办理护栏：唤醒轮白名单收窄、日程冲突检测与可撤回纠错动作（ADR 0024 / 0025）。

- 被匠人回调唤醒的复核轮可能携带外部不可信文本，为防二阶提示注入，
  ``act_personal`` 仅允许纯追加类安全写操作，禁止覆盖、完成或删除已有个人记录；
- 创建/修改日程时由代码确定性计算时间重叠（``conflicts``）与 60 分钟通勤/衔接缓冲
  （``adjacent_events``），绝不依赖模型口算；
- 纠错类动作（``cancel_event`` / ``delete_todo``）走软回收站，支持一键 ``/undo`` 恢复。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from server.core.errors import InvalidRequestError, NotFoundError

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

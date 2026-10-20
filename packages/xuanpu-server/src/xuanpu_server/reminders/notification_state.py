"""统一通知当前业务来源的纯判断，历史记录与发送资格分别保存。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xuanpu_server.reminders.models import NotificationRecord


def target_current(source: dict[str, Any], item: dict[str, Any] | None) -> bool:
    """关联待办必须仍存在且未完成，目标代际改变后旧判断失效。"""
    if source.get("item_id") is None:
        return True
    if item is None or item.get("trashed_at") is not None or item["data"].get("status") == "done":
        return False
    return source.get("generation", item.get("generation")) == item.get("generation")


def proactive_notification_current(
    notification: NotificationRecord,
    source: dict[str, Any] | None,
    item: dict[str, Any] | None,
) -> bool:
    """只有当前待核实阶段可提示，等待新时间和已处理阶段保留历史但失去发送资格。"""
    if source is None or source.get("status") not in ("awaiting_user", "notified"):
        return False
    if source.get("handled_at") is not None and notification.created_at <= source["handled_at"]:
        return False
    if source.get("last_notified_at", notification.created_at) != notification.created_at:
        return False
    if "due_at" in source and source["due_at"] != notification.due_at:
        return False
    return target_current(source, item)


def native_notification_current(
    notification: NotificationRecord,
    reminder_status: str | None,
    sources: list[tuple[dict[str, Any], dict[str, Any] | None]],
) -> bool:
    """手工提醒保留既有语义，主动安排必须仍有效且对应原来的跟进时间。"""
    if reminder_status == "cancelled":
        return False
    if not sources:
        return True
    return any(
        source.get("status") in ("waiting", "awaiting_user", "notified")
        and source.get("due_at", source.get("next_review_at", notification.due_at))
        == notification.due_at
        and target_current(source, item)
        for source, item in sources
    )

"""确定性关注阶段、免打扰时间与稳定业务标识。"""
from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

OVERDUE = (0, 3600, 86400, 259200)
STALE = (259200, 604800, 1209600)
TERMINAL = {"resolved", "dismissed", "ignored"}


def stable_id(prefix: str, *values: object) -> str:
    """稳定标识由平台计算，不接受模型自选去重键。"""
    encoded = "\x00".join(str(value) for value in values).encode()
    return prefix + hashlib.sha256(encoded).hexdigest()[:32]


def enabled(settings: dict[str, Any], kind: str, now: float) -> bool:
    """用户关闭与持续忽略始终优先于自动识别。"""
    config = settings.get("proactive_checks", {})
    if not config.get("enabled", True):
        return False
    return not any(
        ignore["type"] == kind and (ignore.get("until") is None or ignore["until"] > now)
        for ignore in config.get("ignored_types", [])
    )


def alert_time(settings: dict[str, Any], now: float) -> float:
    """免打扰只延迟悬浮与外部发送，站内通知仍立即保存。"""
    quiet = settings.get("proactive_checks", {}).get("quiet_hours", {})
    if not quiet.get("enabled", False):
        return now
    zone = ZoneInfo(settings.get("timezone", "Asia/Shanghai"))
    local = datetime.fromtimestamp(now, zone)
    start, end = time.fromisoformat(quiet["start"]), time.fromisoformat(quiet["end"])
    current = local.time().replace(tzinfo=None)
    inside = start <= current < end if start < end else current >= start or current < end
    if not inside:
        return now
    day = local.date() + timedelta(days=1 if start >= end and current >= start else 0)
    return max(now, datetime.combine(day, end, zone).timestamp())


def todo_check(item: dict[str, Any], now: float) -> dict[str, Any] | None:
    """只判断已记录的状态，描述未记录进展而非推断现实未完成。"""
    if item["kind"] != "todo" or item.get("trashed_at") is not None or item["data"]["status"] != "open":
        return None
    due = item["data"].get("due_at")
    basis = due if due is not None else item.get("last_progress_at", item["created_at"])
    kind, thresholds = ("overdue", OVERDUE) if due is not None else ("stale", STALE)
    stages = [i for i, value in enumerate(thresholds) if now >= basis + value]
    if not stages:
        return None
    index = max(stages)
    if due is not None:
        reason = "这项待办已到原定时间，尚未标记完成。现在进展如何？"
    else:
        days = thresholds[index] // 86400
        reason = f"这项待办已有 {days} 天没有记录到新的业务进展。需要继续处理或调整安排吗？"
    return {
        "item_id": item["id"], "item_version": item["version"], "title": item["data"]["title"],
        "kind": kind, "check_type": kind, "generation": item.get("generation", 1), "stage_index": index,
        "basis_at": basis, "threshold_seconds": thresholds[index], "reason": reason,
        "body": reason, "due_at": due, "certainty": "explicit",
        "evidence_refs": [f"item:{item['id']}:{item['version']}"],
    }

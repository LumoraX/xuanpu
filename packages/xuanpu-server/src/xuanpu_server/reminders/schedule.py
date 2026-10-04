"""按当地墙上时钟推进日/周提醒，跳过 DST 缺失时刻。"""

from __future__ import annotations

from calendar import isleap
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from xuanpu_server.errors import InvalidRequestError


def validate_due(due_at: float, now: float) -> None:
    """首次保存必须在未来五年内。"""
    current = datetime.fromtimestamp(now, UTC)
    year = current.year + 5
    day = 28 if current.month == 2 and current.day == 29 and not isleap(year) else current.day
    horizon = current.replace(year=year, day=day).timestamp()
    if not now < due_at <= horizon:
        raise InvalidRequestError("提醒时间必须在未来五年内")


def next_due(due_at: float, timezone: str, repeat: str, now: float) -> float | None:
    """合并错过的重复提醒，返回严格晚于当前时刻的下一有效当地时间。"""
    if repeat == "once":
        return None
    zone = ZoneInfo(timezone)
    local = datetime.fromtimestamp(due_at, zone)
    current = datetime.fromtimestamp(now, zone)
    step = 1 if repeat == "daily" else 7
    days = max(step, ((current.date() - local.date()).days // step) * step)
    candidate = local.replace(tzinfo=None) + timedelta(days=days)
    # 每步按日历走，fold=0 保证重叠时间只取第一次；往返不同即为不存在的时间。
    while True:
        aware = candidate.replace(tzinfo=zone, fold=0)
        stamp = aware.timestamp()
        roundtrip = datetime.fromtimestamp(stamp, zone).replace(tzinfo=None)
        if stamp > now and roundtrip == candidate:
            return stamp
        candidate += timedelta(days=step)


def latest_due(due_at: float, timezone: str, repeat: str, now: float) -> float:
    """周期错过时取最近一次有效当地时间，保留原日历时刻和DST规则。"""
    if repeat == "once":
        return due_at
    zone = ZoneInfo(timezone)
    local = datetime.fromtimestamp(due_at, zone)
    current = datetime.fromtimestamp(now, zone)
    step = 1 if repeat == "daily" else 7
    days = max(0, ((current.date() - local.date()).days // step) * step)
    candidate = local.replace(tzinfo=None) + timedelta(days=days)
    while True:
        stamp = candidate.replace(tzinfo=zone, fold=0).timestamp()
        roundtrip = datetime.fromtimestamp(stamp, zone).replace(tzinfo=None)
        if stamp <= now and roundtrip == candidate:
            return max(due_at, stamp)
        candidate -= timedelta(days=step)

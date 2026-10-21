"""复用提醒的IANA日历，不实现通用cron或无限追赶。"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.reminders.schedule import latest_due, next_due


def parse_time(value: str, timezone: str) -> float:
    """明确偏移且与指定IANA时区一致，拒绝不存在的当地时刻。"""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.microsecond:
            raise ValueError('时刻须带偏移且精确到整秒')
        actual = datetime.fromtimestamp(parsed.timestamp(),ZoneInfo(timezone))
        if actual.utcoffset()!=parsed.utcoffset() or actual.replace(tzinfo=None)!=parsed.replace(tzinfo=None):
            raise ValueError('时刻与指定时区不一致')
        if not 0 <= parsed.timestamp() <= 253402300799:
            raise ValueError('时间超限')
        return parsed.timestamp()
    except (ValueError,OverflowError) as error:
        raise InvalidRequestError('执行时间必须包含有效偏移并匹配指定时区') from error


def time_occurrence_key(definition_id: str,version: int,scheduled: datetime) -> str:
    """用UTC整秒形成稳定键；时区显示和时钟倒退不改变它。"""
    if scheduled.tzinfo is None or scheduled.microsecond:
        raise ValueError('计划时刻必须明确到带时区的整秒')
    return f'time:{definition_id}:{version}:{int(scheduled.astimezone(UTC).timestamp())}'


def time_plan(first: float,timezone: str,repeat: str,now: float,policy: str) -> tuple[float,float | None,str]:
    """最多补最近一次，错过窗口记录missed，不枚举全部历史。"""
    due=latest_due(first,timezone,repeat,now) if policy=='coalesce_latest' else first
    status='missed' if policy=='skip' and now-due>900. else 'pending'
    return due,next_due(first,timezone,repeat,now),status


def recurring_first(value: str, timezone: str) -> float:
    """首次重复锚点拒绝fold晚侧，后续周期沿用共用日历的较早一次。"""
    stamp = parse_time(value, timezone)
    if datetime.fromtimestamp(stamp, ZoneInfo(timezone)).fold:
        raise InvalidRequestError('重复安排的重叠时刻须选择较早偏移')
    return stamp

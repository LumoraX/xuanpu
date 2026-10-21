"""共用日历的DST、明确偏移和有界错过语义。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from xuanpu_server.errors import InvalidRequestError


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def calendar():
    """失败先指出缺少触发日历，而不把导入错误当业务断言。"""
    path=Path(__file__).resolve().parents[2]/'src/xuanpu_server/task_triggers/calendar.py'
    assert path.exists(), 'R2-B尚无日历触发契约'
    from xuanpu_server.task_triggers import calendar as module
    return module


def test_dst_gap_skips_and_fold_occurs_only_once():
    c=calendar()
    first=stamp('2026-03-07T02:30:00-05:00')
    _,next_fire,status=c.time_plan(first,'America/New_York','daily',first,'skip')
    assert next_fire==stamp('2026-03-09T02:30:00-04:00') and status=='pending'
    before=stamp('2026-10-31T01:30:00-04:00')
    _,fold,_=c.time_plan(before,'America/New_York','daily',before,'skip')
    assert fold==stamp('2026-11-01T01:30:00-04:00')
    _,after,_=c.time_plan(fold,'America/New_York','daily',fold,'skip')
    assert after==stamp('2026-11-02T01:30:00-05:00')


def test_three_days_misfire_coalesces_at_most_once_and_keys_are_utc():
    c=calendar()
    first=stamp('2026-10-21T09:00:00+08:00')
    now=stamp('2026-10-24T10:00:00+08:00')
    due,next_fire,status=c.time_plan(first,'Asia/Shanghai','daily',now,'skip')
    assert due==first and status=='missed' and next_fire>now
    due,next_fire,status=c.time_plan(first,'Asia/Shanghai','daily',now,'coalesce_latest')
    assert due==stamp('2026-10-24T09:00:00+08:00') and status=='pending' and next_fire>now
    assert c.time_occurrence_key('def',1,datetime.fromisoformat('2026-10-21T09:00:00+08:00'))==f'time:def:1:{int(first)}'
    with pytest.raises(InvalidRequestError):c.parse_time('2026-03-08T02:30:00-05:00','America/New_York')
    with pytest.raises(InvalidRequestError):c.parse_time('2026-10-21T09:00:00','Asia/Shanghai')


@pytest.mark.parametrize('repeat,first,after,expected',[
    ('weekly','2026-12-28T09:00:00+08:00','2026-12-28T09:00:00+08:00','2027-01-04T09:00:00+08:00'),
    ('daily','2026-12-31T09:00:00+08:00','2026-12-31T09:00:00+08:00','2027-01-01T09:00:00+08:00'),
    ('weekly','2026-03-01T02:30:00-05:00','2026-03-01T02:30:00-05:00','2026-03-15T02:30:00-04:00'),
])
def test_week_year_and_dst_boundaries(repeat,first,after,expected):
    _,next_fire,_=calendar().time_plan(stamp(first),'America/New_York' if '-05:00' in first else 'Asia/Shanghai',repeat,stamp(after),'skip')
    assert next_fire==stamp(expected)


def test_recurring_first_fold_must_use_earlier_instant():
    """首次重复锚点也遵守fold较早一次；一次性明确偏移仍可选择晚侧。"""
    c=calendar()
    assert c.parse_time('2026-11-01T01:30:00-05:00','America/New_York')>0
    assert hasattr(c,'recurring_first'),'首次重复锚点尚未核实fold'
    with pytest.raises(InvalidRequestError):
        c.recurring_first('2026-11-01T01:30:00-05:00','America/New_York')
    assert c.recurring_first('2026-11-01T01:30:00-04:00','America/New_York')==stamp('2026-11-01T01:30:00-04:00')

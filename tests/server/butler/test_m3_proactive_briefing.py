"""SOUL M3：Attention Budget 门控、后台配额分级降级、习惯校准、会前 30 分钟速览与每日晨报测试。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from server.engine.butler.proactive_briefing import (
    AttentionBudgetGate,
    AttentionBudgetPolicy,
    HabitCalibration,
)


def test_attention_budget_quiet_hours_quota_degradation_and_habit_calibration() -> None:
    """验证静音时段拦截、后台配额 70%/85%/100% 分级降级、冷却期与撤回/忽略习惯校准。"""
    gate = AttentionBudgetGate(AttentionBudgetPolicy(
        quiet_start_hour=22,
        quiet_end_hour=8,
        max_daily_pushes=2,
        cooldown_seconds=1800.0,
        daily_bg_token_cap=100_000,
    ))
    tz = "Asia/Shanghai"
    night = datetime(2026, 11, 27, 23, 30, tzinfo=ZoneInfo(tz)).timestamp()
    morning = datetime(2026, 11, 27, 9, 0, tzinfo=ZoneInfo(tz)).timestamp()

    # 1. 静音时段：每日晨报被拦截，但刚需会前速览可放行
    assert gate.evaluate("u1", priority="daily_brief", topic_key="d1", now=night, timezone=tz).reason == "quiet_hours"
    assert gate.evaluate("u1", priority="pre_meeting_brief", topic_key="m1", now=night, timezone=tz).allowed is True

    # 2. 后台配额分级降级：75% 暂停 topic_watch，90% 仅放行 pre_meeting_brief，100% 全停
    d75 = gate.evaluate("u1", priority="topic_watch", topic_key="t1", now=morning, timezone=tz, daily_bg_tokens_used=75_000)
    assert not d75.allowed and d75.degraded is True
    assert gate.evaluate("u1", priority="daily_brief", topic_key="d2", now=morning, timezone=tz, daily_bg_tokens_used=75_000).allowed is True
    assert not gate.evaluate("u1", priority="daily_brief", topic_key="d2", now=morning, timezone=tz, daily_bg_tokens_used=90_000).allowed
    assert gate.evaluate("u1", priority="pre_meeting_brief", topic_key="m2", now=morning, timezone=tz, daily_bg_tokens_used=90_000).allowed is True
    assert not gate.evaluate("u1", priority="pre_meeting_brief", topic_key="m2", now=morning, timezone=tz, daily_bg_tokens_used=100_000).allowed

    # 3. 冷却期与日触达上限
    gate.record_sent("u1", topic_key="same_topic", now=morning, timezone=tz)
    assert gate.evaluate("u1", priority="daily_brief", topic_key="same_topic", now=morning + 60, timezone=tz).reason == "topic_cooldown"
    gate.record_sent("u1", topic_key="other_topic", now=morning + 120, timezone=tz)
    assert gate.evaluate("u1", priority="daily_brief", topic_key="third", now=morning + 200, timezone=tz).reason == "daily_push_cap_reached"

    # 4. 习惯校准：高撤回率降级草稿；连续忽略 >= 3 自动静音
    habit = HabitCalibration(
        action_undo_counts={"create_event": (2, 4), "create_todo": (0, 5)},
        topic_ignored_streaks={"topic_watch": 3},
    )
    assert habit.should_downgrade_to_draft("create_event") is True
    assert habit.should_downgrade_to_draft("create_todo") is False
    assert gate.evaluate("u2", priority="topic_watch", topic_key="w1", now=morning, timezone=tz, habit=habit).reason == "habit_muted"


async def test_pre_meeting_and_daily_briefing_endpoints(butler: dict[str, Any]) -> None:
    """会前 30 分钟速览聚合关联项目与待办；每日晨报检测冲突并按单人格投递且幂等去重。"""
    platform = butler["world"].platform
    alice = butler["alice"]
    tz = "Asia/Shanghai"
    now = datetime(2026, 11, 27, 9, 0, tzinfo=ZoneInfo(tz)).timestamp()
    platform.butler.briefings._now = lambda: now

    matter = await platform.workspace.create(alice, "matter", {"title": "华东客户合作", "status": "active"}, "m_seed")
    await platform.workspace.create(
        alice, "todo",
        {"title": "确认报价底线", "status": "open", "due_at": now + 3600, "matter_id": matter["id"]},
        "t_seed",
    )
    # 20 分钟后开始的会议 + 与之冲突的重叠日程
    await platform.workspace.create(
        alice, "event",
        {"title": "客户高层沟通", "start_at": now + 1200, "end_at": now + 4800, "timezone": tz, "matter_id": matter["id"]},
        "e_seed_1",
    )
    await platform.workspace.create(
        alice, "event",
        {"title": "内部评审", "start_at": now + 2400, "end_at": now + 5400, "timezone": tz},
        "e_seed_2",
    )

    # 1. 会前 30 分钟速览接口（deliver=true 写入单人格主对话）
    pm = await butler["http"].get("/v1/assistant/briefings/pre-meeting?window_minutes=30&deliver=true")
    assert pm.status_code == 200, pm.text
    items = pm.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "客户高层沟通"
    assert "确认报价底线" in items[0]["summary"]
    assert items[0]["delivered"] is True

    # 2. 每日要点晨报接口（检测到冲突 + 1–3 条重点待办）
    daily = await butler["http"].get("/v1/assistant/briefings/daily?deliver=true")
    assert daily.status_code == 200, daily.text
    body = daily.json()
    assert len(body["conflicts"]) == 1
    assert "时间重叠" in body["summary"]
    assert "确认报价底线" in body["summary"]
    assert body["delivered"] is True

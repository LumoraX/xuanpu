"""管家单人格主动简报引擎：注意力预算（Attention Budget）、后台配额分级降级、会前 30 分钟速览与每日晨报（SOUL M3）。

- 所有规则扫描（日程临近、冲突检测、逾期待办、生日临近、关联人脉与历史纪要）均为确定性 0-LLM 扫描；
- 通过全局 ``AttentionBudgetGate`` 严格遵守静音时段（``quiet_hours``）、日触达上限、主题冷却期，
  以及按「会前速览 > 每日晨报 > 周期复盘 > 长期静默追踪」分级的独立后台配额降级，绝不挤占实时对话；
- 内置隐式习惯校准（``HabitCalibration``）：撤回率偏高自动建议降级草稿，连续忽略自动静音。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from server.domains.dashboard.service import day_bounds
from server.engine.butler.personal_guardrails import detect_event_conflicts

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.core.tenancy import Principal
    from server.platform import Platform

BriefPriority = Literal["pre_meeting_brief", "daily_brief", "periodic_review", "topic_watch"]

_PRIORITY_RANK: dict[BriefPriority, int] = {
    "pre_meeting_brief": 1,
    "daily_brief": 2,
    "periodic_review": 3,
    "topic_watch": 4,
}


class AttentionBudgetPolicy(BaseModel):
    """全局注意力预算与后台独立配额护栏策略。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    quiet_start_hour: int = Field(default=22, ge=0, le=23)
    quiet_end_hour: int = Field(default=8, ge=0, le=23)
    max_daily_pushes: int = Field(default=6, ge=1, le=50)
    cooldown_seconds: float = Field(default=3600.0, ge=0.0, le=86400.0)
    daily_bg_token_cap: int = Field(default=200_000, ge=1_000, le=50_000_000)


class AttentionDecision(BaseModel):
    """注意力预算门控判定结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    allowed: bool
    reason: str
    degraded: bool = False


class HabitCalibration(BaseModel):
    """主人隐式习惯校准：按撤回率与连续忽略次数自动调整主动度。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    action_undo_counts: dict[str, tuple[int, int]] = Field(default_factory=dict)  # category -> (undone, total)
    topic_ignored_streaks: dict[str, int] = Field(default_factory=dict)

    def should_downgrade_to_draft(self, category: str) -> bool:
        """某类自主写入近期待撤回率 >= 40%（且样本 >= 3）时降级为草稿确认。"""
        undone, total = self.action_undo_counts.get(category, (0, 0))
        return total >= 3 and (undone / total) >= 0.4

    def should_mute_topic(self, topic_kind: str) -> bool:
        """某类主动推送连续被忽略 >= 3 次时自动静音。"""
        return self.topic_ignored_streaks.get(topic_kind, 0) >= 3


class AttentionBudgetGate:
    """按员工隔离的注意力预算与后台配额分级降级门控器。"""

    def __init__(self, policy: AttentionBudgetPolicy | None = None) -> None:
        """初始化策略与按员工维度的日推送/冷却记忆。"""
        self.policy = policy or AttentionBudgetPolicy()
        self._daily_counts: dict[tuple[str, str], int] = {}
        self._cooldowns: dict[tuple[str, str], float] = {}

    def _in_quiet_hours(self, now: float, timezone: str) -> bool:
        """判断当前员工本地时间是否处于免打扰静音时段。"""
        hour = datetime.fromtimestamp(now, ZoneInfo(timezone)).hour
        start, end = self.policy.quiet_start_hour, self.policy.quiet_end_hour
        if start == end:
            return False
        if start > end:
            return hour >= start or hour < end
        return start <= hour < end

    def evaluate(
        self,
        user_key: str,
        *,
        priority: BriefPriority,
        topic_key: str,
        now: float,
        timezone: str = "Asia/Shanghai",
        daily_bg_tokens_used: int = 0,
        habit: HabitCalibration | None = None,
    ) -> AttentionDecision:
        """综合静音时段、习惯静音、后台配额分级降级、日触达上限与冷却期判定是否允许推送。"""
        if habit is not None and habit.should_mute_topic(priority):
            return AttentionDecision(allowed=False, reason="habit_muted", degraded=True)

        # 后台独立配额优先级降级：实时对话 > 会前 30 分钟速览 > 每日晨报 > 周期复盘 > 长期静默追踪
        ratio = daily_bg_tokens_used / max(1, self.policy.daily_bg_token_cap)
        if ratio >= 1.0:
            return AttentionDecision(allowed=False, reason="bg_quota_exhausted", degraded=True)
        if ratio >= 0.85 and _PRIORITY_RANK[priority] >= 2:
            return AttentionDecision(allowed=False, reason="bg_quota_degraded_only_pre_meeting", degraded=True)
        if ratio >= 0.70 and _PRIORITY_RANK[priority] >= 4:
            return AttentionDecision(allowed=False, reason="bg_quota_degraded_pause_topic_watch", degraded=True)

        # 会前 30 分钟刚需速览可穿透普通静音时段；其余低优主动推送遵守 quiet_hours
        if priority != "pre_meeting_brief" and self._in_quiet_hours(now, timezone):
            return AttentionDecision(allowed=False, reason="quiet_hours")

        day_str = datetime.fromtimestamp(now, ZoneInfo(timezone)).strftime("%Y-%m-%d")
        used = self._daily_counts.get((user_key, day_str), 0)
        if used >= self.policy.max_daily_pushes:
            return AttentionDecision(allowed=False, reason="daily_push_cap_reached")

        last_sent = self._cooldowns.get((user_key, topic_key))
        if last_sent is not None and (now - last_sent) < self.policy.cooldown_seconds:
            return AttentionDecision(allowed=False, reason="topic_cooldown")

        return AttentionDecision(allowed=True, reason="ok")

    def record_sent(self, user_key: str, *, topic_key: str, now: float, timezone: str = "Asia/Shanghai") -> None:
        """登记一次已放行的主动推送计数与冷却时间戳。"""
        day_str = datetime.fromtimestamp(now, ZoneInfo(timezone)).strftime("%Y-%m-%d")
        self._daily_counts[(user_key, day_str)] = self._daily_counts.get((user_key, day_str), 0) + 1
        self._cooldowns[(user_key, topic_key)] = now


class ProactiveBriefingService:
    """确定性会前 30 分钟速览与高信噪比每日晨报服务（统一以管家单人格输出）。"""

    def __init__(
        self,
        platform: Platform,
        *,
        gate: AttentionBudgetGate | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """注入平台、注意力门控与时钟。"""
        self._platform = platform
        self.gate = gate or AttentionBudgetGate()
        self._now = now

    async def build_pre_meeting_briefs(
        self,
        who: Principal,
        *,
        timezone: str = "Asia/Shanghai",
        window_minutes: int = 30,
        deliver: bool = False,
        daily_bg_tokens_used: int = 0,
    ) -> list[dict[str, Any]]:
        """扫描未来 ``window_minutes`` 分钟内即将开始的日程，聚合关联项目、参会人与未完待办。"""
        now = self._now()
        horizon = now + max(5, min(window_minutes, 180)) * 60.0
        events = await self._platform.workspace.list(who, kind="event")
        upcoming = [
            ev for ev in events
            if now < float(ev["data"]["start_at"]) <= horizon
        ]
        upcoming.sort(key=lambda ev: float(ev["data"]["start_at"]))
        briefs: list[dict[str, Any]] = []
        user_key = f"{who.tenant_id}:{who.user_id}"

        for ev in upcoming[:3]:
            data = ev["data"]
            matter_id = data.get("matter_id")
            people: list[str] = []
            open_todos: list[str] = []
            if matter_id:
                try:
                    linked = await self._platform.people.list_matter_people(who, str(matter_id))
                    people = [str(p.get("name") or "") for p in linked[:5] if p.get("name")]
                except Exception:
                    people = []
                todos = await self._platform.workspace.list(who, kind="todo")
                open_todos = [
                    str(t["data"].get("title") or "")
                    for t in todos
                    if t["data"].get("status") == "open" and t["data"].get("matter_id") == matter_id
                ][:3]
            minutes_left = max(1, round((float(data["start_at"]) - now) / 60.0))
            lines = [f"【会前 {minutes_left} 分钟速览】{data['title']}"]
            if people:
                lines.append(f"- 关联人物：{'、'.join(people)}")
            if open_todos:
                lines.append(f"- 待核对待办：{'；'.join(open_todos)}")
            if data.get("description"):
                lines.append(f"- 议程备注：{str(data['description'])[:180]}")
            summary = "\n".join(lines)
            entry = {
                "event_id": ev["id"],
                "title": data["title"],
                "start_at": data["start_at"],
                "minutes_left": minutes_left,
                "people": people,
                "open_todos": open_todos,
                "summary": summary,
                "delivered": False,
            }
            if deliver:
                decision = self.gate.evaluate(
                    user_key,
                    priority="pre_meeting_brief",
                    topic_key=f"pre_meeting:{ev['id']}",
                    now=now,
                    timezone=timezone,
                    daily_bg_tokens_used=daily_bg_tokens_used,
                )
                entry["attention_decision"] = decision.model_dump()
                if decision.allowed:
                    await self._platform.workspace.append_message(
                        who, "assistant", summary, f"butler:pre_meeting:{ev['id']}",
                        metadata={"butler": {"event": "pre_meeting_brief", "event_id": ev["id"]}},
                    )
                    self.gate.record_sent(user_key, topic_key=f"pre_meeting:{ev['id']}", now=now, timezone=timezone)
                    entry["delivered"] = True
            briefs.append(entry)
        return briefs

    async def build_daily_brief(
        self,
        who: Principal,
        *,
        timezone: str = "Asia/Shanghai",
        deliver: bool = False,
        daily_bg_tokens_used: int = 0,
    ) -> dict[str, Any]:
        """按本人时区聚合高信噪比「今日要点晨报」（每类严格封顶 1–3 项，绝不清单轰炸）。"""
        now = self._now()
        start, end = day_bounds(now, timezone)
        day_str = datetime.fromtimestamp(now, ZoneInfo(timezone)).strftime("%Y-%m-%d")

        # 1. 今日日程及冲突预警
        events = [
            ev for ev in await self._platform.workspace.list(who, kind="event")
            if float(ev["data"]["start_at"]) < end and float(ev["data"]["end_at"]) > start
        ]
        events.sort(key=lambda e: float(e["data"]["start_at"]))
        conflict_pairs: list[str] = []
        seen_pairs: set[tuple[str, str]] = set()
        for ev in events:
            chk = await detect_event_conflicts(
                self._platform, who,
                start_at=float(ev["data"]["start_at"]),
                end_at=float(ev["data"]["end_at"]),
                exclude_id=ev["id"],
            )
            for c in chk["conflicts"]:
                pair = tuple(sorted((ev["id"], str(c["id"]))))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    conflict_pairs.append(f"「{ev['data']['title']}」与「{c['title']}」时间重叠")

        # 2. 最关键的 1–3 条今日/逾期待办
        todos = [
            t for t in await self._platform.workspace.list(who, kind="todo")
            if t["data"].get("status") == "open"
            and t["data"].get("due_at") is not None
            and float(t["data"]["due_at"]) < end
        ]
        todos.sort(key=lambda t: float(t["data"]["due_at"]))
        top_todos = [
            {"id": t["id"], "title": t["data"]["title"], "overdue": float(t["data"]["due_at"]) < start}
            for t in todos[:3]
        ]

        # 3. 活跃提醒（今日到期，最多 3 条）
        reminders = [
            {"reminder_id": r.reminder_id, "title": r.title, "due_at": r.due_at}
            for r in await self._platform.reminders.list(who, status="active")
            if r.due_at < end
        ][:3]

        lines = [f"【今日要点 · {day_str}】"]
        if conflict_pairs:
            lines.append("- 日程冲突预警：" + "；".join(conflict_pairs[:3]))
        if top_todos:
            todo_txt = "；".join(("（逾期）" if t["overdue"] else "") + str(t["title"]) for t in top_todos)
            lines.append("- 优先推进待办：" + todo_txt)
        if reminders:
            lines.append("- 今日提醒：" + "；".join(str(r["title"]) for r in reminders))
        if len(lines) == 1:
            lines.append("- 今日暂无日程冲突或紧迫到期项，局面清爽。")
        summary = "\n".join(lines)

        result: dict[str, Any] = {
            "date": day_str,
            "timezone": timezone,
            "event_count": len(events),
            "conflicts": conflict_pairs[:3],
            "top_todos": top_todos,
            "reminders": reminders,
            "summary": summary,
            "delivered": False,
        }
        if deliver:
            user_key = f"{who.tenant_id}:{who.user_id}"
            decision = self.gate.evaluate(
                user_key,
                priority="daily_brief",
                topic_key=f"daily_brief:{day_str}",
                now=now,
                timezone=timezone,
                daily_bg_tokens_used=daily_bg_tokens_used,
            )
            result["attention_decision"] = decision.model_dump()
            if decision.allowed:
                await self._platform.workspace.append_message(
                    who, "assistant", summary, f"butler:daily_brief:{day_str}",
                    metadata={"butler": {"event": "daily_brief", "date": day_str}},
                )
                self.gate.record_sent(user_key, topic_key=f"daily_brief:{day_str}", now=now, timezone=timezone)
                result["delivered"] = True
        return result

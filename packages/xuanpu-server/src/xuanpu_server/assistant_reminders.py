"""模型只能提出提醒草稿，时间校验与创建权限由玄圃控制。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import SessionRecord

REMINDER_PACKAGE = "@reminders"
REMINDER_ENTRIES = [
    {"package": REMINDER_PACKAGE, "version": "1", "entry": "create",
     "description": "玄圃内置个人提醒，无需外部平台", "entry_description": "一次、每日、每周定时提醒，生成待确认草稿"},
    {"package": REMINDER_PACKAGE, "version": "1", "entry": "manage",
     "description": "玄圃内置个人提醒", "entry_description": "查看、修改和取消我的提醒，打开本人提醒管理"},
]


class ReminderProposal(BaseModel):
    """模型的有界建议，禁止指定租户、用户、脚本或写入权限。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=500, pattern=r"\S")
    due_at: str = Field(min_length=10, max_length=64)
    timezone: str = Field(max_length=64)
    repeat: Literal["once", "daily", "weekly"] = "once"

    def preview(self, now: float) -> dict[str, Any]:
        """将明确的绝对时间转为UTC秒；拒绝过去时间、无时区和时区偏移不符。"""
        due = datetime.fromisoformat(self.due_at)
        zone = ZoneInfo(self.timezone)
        if due.tzinfo is None or due.utcoffset() != due.astimezone(zone).utcoffset():
            raise ValueError("提醒时间必须包含与所选时区一致的偏移")
        stamp = due.timestamp()
        if not now < stamp <= now + 5 * 366 * 86400:
            raise ValueError("提醒时间须在未来五年内")
        return {"title": self.title, "due_at": stamp, "timezone": self.timezone,
                "repeat": self.repeat, "channels": ["inbox"]}


@dataclass(frozen=True)
class RouteOutcome:
    """路由结果：会话、解释或不产生写入的提醒草稿。"""
    session: SessionRecord | None = None
    message: str | None = None
    reminder_draft: dict[str, Any] | None = None
    open_reminders: bool = False
    workspace_draft: dict[str, Any] | None = None
    open_workspace: str | None = None
    proposal_id: str | None = None
    proactive_draft: dict[str, Any] | None = None
    open_proactive: bool = False
    work_task_id: str | None = None
    preserve_input: bool = False

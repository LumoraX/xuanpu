"""主动业务的严格模型输出和用户确认输入。"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xuanpu_server.reminders.models import default_channels
from xuanpu_server.workspace.models import timezone_name


class EvidenceCandidate(BaseModel):
    """模型只能提出带真实证据的候选，不能输出执行权限。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=500, pattern=r"\S")
    commitment_index: int = Field(default=0, ge=0, le=20)
    intent_type: Literal["commitment", "waiting_result", "stalled", "missing_schedule"]
    certainty: Literal["explicit", "inferred"]
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    evidence_refs: list[str] = Field(min_length=1, max_length=12)
    item_id: str | None = Field(default=None, max_length=128)
    followup_id: str | None = Field(default=None, max_length=128)
    proposed_time: str | None = Field(default=None, max_length=80)
    next_review_at: str | None = Field(default=None, max_length=80)
    suggested_action: Literal["ask_status", "set_reminder", "prepare", "complete", "dismiss"] = "ask_status"


class Assessment(BaseModel):
    """一次有界评估，安静状态用空数组明确表达。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    followups: list[EvidenceCandidate] = Field(max_length=20)


class ActionInput(BaseModel):
    """来自用户明确点击或确认的操作，不接受租户和员工字段。"""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")
    version: int = Field(ge=1)
    item_version: int | None = Field(default=None, ge=1)
    action: Literal["complete", "postpone", "ignore_stage", "set_reminder", "progress"]
    due_at: float | None = Field(default=None, ge=0, le=253402300799)
    timezone: str = "Asia/Shanghai"
    channels: list[Literal["inbox", "feishu"]] = Field(default_factory=default_channels)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """使用同一 IANA 时区库，拒绝无效名字。"""
        return timezone_name(value)

    @field_validator("channels")
    @classmethod
    def valid_channels(cls, value: list[Literal["inbox", "feishu"]]) -> list[Literal["inbox", "feishu"]]:
        """站内通知必须保留，不能重复指定渠道。"""
        if "inbox" not in value or len(set(value)) != len(value):
            raise ValueError("通知渠道须包含站内通知且不能重复")
        return value


def absolute_time(value: str | None) -> float | None:
    """模型相对时间必须转换成带偏移绝对时间再进入领域层。"""
    if value is None:
        return None
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("时间必须带 UTC 偏移")
    stamp = result.timestamp()
    if not math.isfinite(stamp) or not 0 <= stamp <= 253402300799:
        raise ValueError("时间超出支持范围")
    return stamp


class ProactiveProposal(BaseModel):
    """模型提出待用户确认的动作，不能自行产生业务写入。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    action: Literal["complete", "postpone", "ignore_stage", "set_reminder", "progress"]
    due_at: str | None = Field(default=None, max_length=80)
    timezone: str = "Asia/Shanghai"


PROACTIVE_ENTRIES = [
    {"package": "@proactive", "version": "1", "entry": "manage", "description": "玄圃主动业务助理",
     "entry_description": "查看业务跟进、核实承诺、长期未处理和可能遗漏的工作"},
    {"package": "@proactive", "version": "1", "entry": "action", "description": "确认业务跟进决定",
     "entry_description": "已完成、延后、记录进展、忽略、安排提醒，只提出待确认动作"},
]
PROACTIVE_ROUTING = """
主动业务是玄圃内置能力。proactive_context 包含本人真实跟进。
查看业务跟进选择@proactive/manage，proactive=null。
用户明确要求已完成/延后/不用跟进/设置提醒时，选择@proactive/action，附加proactive：
{"id":"上下文中的真实id","version":真实version,"action":"complete|postpone|ignore_stage|set_reminder|progress",
"due_at":"含UTC偏移ISO8601或null","timezone":"IANA时区"}。
message=null。只有具体指向一个真实跟进且用户已作出决定时才能提出动作；引用不明确先追问。
这是建议卡，等待用户确认后才能实际更新，不得声称已经完成。其他选择不要携带proactive。
"""

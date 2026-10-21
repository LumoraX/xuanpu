"""玄圃原生提醒输入、持久记录与外部发送协议。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from xuanpu_server.tenancy import Principal

Repeat = Literal["once", "daily", "weekly"]
Channel = Literal["inbox", "feishu"]


def default_channels() -> list[Channel]:
    """默认只启用核心站内信。"""
    return ["inbox"]


class ReminderInput(BaseModel):
    """只允许员工提交提醒内容，不接收租户或员工标识。"""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=500)
    due_at: float
    timezone: str
    repeat: Repeat = "once"
    channels: list[Channel] = Field(default_factory=default_channels)

    @field_validator("title")
    @classmethod
    def nonempty_title(cls, value: str) -> str:
        """拒绝纯空白标题并保留原文。"""
        if not value.strip():
            raise ValueError("标题不能为空")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """只接受有效 IANA 时区。"""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("时区无效") from exc
        return value

    @field_validator("channels")
    @classmethod
    def valid_channels(cls, value: list[Channel]) -> list[Channel]:
        """站内信必选，渠道不能重复。"""
        if "inbox" not in value or len(value) != len(set(value)):
            raise ValueError("必须包含站内信且渠道不能重复")
        return sorted(value)

    def fingerprint(self) -> str:
        """计算创建内容指纹，后续编辑不能覆盖。"""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@dataclass(frozen=True)
class ReminderRecord:
    """员工的一条提醒；原创建指纹不随编辑变化。"""

    reminder_id: str
    tenant_id: str
    user_id: str
    title: str
    due_at: float
    timezone: str
    repeat: Repeat
    channels: tuple[Channel, ...]
    status: str
    version: int
    request_id: str
    created_at: float
    updated_at: float
    original_fingerprint: str


@dataclass(frozen=True)
class NotificationRecord:
    """持久站内通知；游标在员工锁内分配以保证提交顺序。"""

    notification_id: int
    tenant_id: str
    user_id: str
    reminder_id: str | None
    title: str
    due_at: float | None
    created_at: float
    read_at: float | None = None
    notification_key: str | None = None
    source: str = "reminder"
    body: str | None = None
    proactive_check_id: str | None = None
    proactive_followup_id: str | None = None
    alert_after: float = 0.0
    task_definition_id: str | None = None


@dataclass(frozen=True)
class DeliveryRecord:
    """外部投递任务；每次认领的令牌隔离过期工作者。"""

    notification_id: int
    tenant_id: str
    user_id: str
    channel: str
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: float = 0.0
    lease_until: float = 0.0
    claim_token: str | None = None
    error_code: str | None = None
    first_attempt_at: float | None = None


class DeliveryError(Exception):
    """发送器的安全错误代码与重试分类，禁止保存供应商原始响应。"""

    def __init__(self, code: str, *, retryable: bool) -> None:
        """代码只接受短小机器标识，不接收供应商正文。"""
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "delivery_error"
        self.retryable = retryable
        super().__init__(self.code)


class NotificationSender(Protocol):
    """可替换的外部渠道适配器；网络调用在事务之外。"""

    async def available(self, who: Principal) -> list[str]:
        """列出该员工已配置且可用的外部渠道。"""
        ...

    async def send(self, notification: NotificationRecord, channel: str) -> None:
        """使用通知标识作为稳定幂等键发送；失败抛 DeliveryError。"""
        ...

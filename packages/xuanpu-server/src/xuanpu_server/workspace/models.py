"""个人工作台的严格领域输入与事务快照。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.reminders.models import default_channels

Name = Annotated[str, Field(min_length=1, max_length=255, pattern=r"\S")]
Title = Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Timestamp = Annotated[float, Field(ge=0, le=253402300799, allow_inf_nan=False)]
KINDS = ("folder", "file", "matter", "todo", "event", "skill")
MAX_FILE_BYTES = 20 * 1024 * 1024
QUOTA_BYTES = 1024 * 1024 * 1024


class StrictModel(BaseModel):
    """不转换类型，不容许未知字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class FolderData(StrictModel):
    """安全显示名称与可选父目录。"""

    name: Name
    parent_id: Identifier | None = None

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        """名字不允许路径、控制字符、首尾空白或特殊目录。"""
        if (
            value != value.strip()
            or value in (".", "..")
            or re.search(r"[/\\\x00-\x1f\x7f]", value)
        ):
            raise ValueError("名称不能包含路径、控制字符或首尾空白")
        return value


class FileData(FolderData):
    """文件元数据；内容派生字段只由上传流程设置。"""

    matter_id: Identifier | None = None
    tags: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=30
    )
    mime_type: str = Field(
        default="application/octet-stream",
        max_length=127,
        pattern=r"^[a-zA-Z0-9!#$&^_.+\-]+/[a-zA-Z0-9!#$&^_.+\-]+$",
    )
    size: int = Field(default=0, ge=0, le=MAX_FILE_BYTES)
    current_revision: int = Field(default=0, ge=0)
    source_url: str | None = Field(default=None, max_length=4096)

    @field_validator("source_url")
    @classmethod
    def https_reference(cls, value: str | None) -> str | None:
        """链接仅保存外部 HTTPS 引用，服务端从不抓取。"""
        if value is not None:
            parsed = TypeAdapter(HttpUrl).validate_python(value)
            if (
                parsed.scheme != "https"
                or not parsed.host
                or parsed.username
                or parsed.password
                or re.search(r"[\x00-\x20\x7f]", value)
            ):
                raise ValueError("仅支持不含凭据的 HTTPS 文件链接")
        return value


class MatterData(StrictModel):
    """事项是本人工作关联的中心。"""

    title: Title
    description: str = Field(default="", max_length=20000)
    status: Literal["active", "completed", "archived"] = "active"


class TodoData(StrictModel):
    """待办只记录完成状态与可选截止时间。"""

    title: Title
    description: str = Field(default="", max_length=20000)
    status: Literal["open", "done"] = "open"
    due_at: Timestamp | None = None
    matter_id: Identifier | None = None


class EventData(StrictModel):
    """日程采用 UTC 秒，时区仅决定显示方式。"""

    title: Title
    description: str = Field(default="", max_length=20000)
    start_at: Timestamp
    end_at: Timestamp
    timezone: str = "Asia/Shanghai"
    matter_id: Identifier | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """只接受系统时区库中的名称。"""
        return timezone_name(value)

    @model_validator(mode="after")
    def chronological(self) -> EventData:
        """结束时间必须晚于开始时间。"""
        if self.end_at <= self.start_at:
            raise ValueError("结束时间必须晚于开始时间")
        return self


class SkillData(StrictModel):
    """个人技能是有界提示说明，不包含执行权限。"""

    title: Title
    description: str = Field(default="", max_length=20000)
    instructions: str = Field(min_length=1, max_length=30000, pattern=r"\S")
    enabled: bool = True


class QuietHours(StrictModel):
    """按本人时区解释的免打扰窗口，允许跨午夜。"""

    enabled: bool = False
    start: str = Field(default="22:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(default="08:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class IgnoredCheckType(StrictModel):
    """本人忽略的检查类型及可空的 UTC 恢复时间。"""

    type: str = Field(min_length=1, max_length=128, pattern=r"\S")
    until: Timestamp | None = None


class ProactiveChecksSettings(StrictModel):
    """主动检查开关、免打扰与个人忽略规则。"""

    enabled: bool = True
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    ignored_types: list[IgnoredCheckType] = Field(default_factory=list, max_length=100)


class SettingsData(StrictModel):
    """个人助手偏好，不覆盖平台授权规则。"""

    assistant_name: str = Field(default="玄圃助手", min_length=1, max_length=80, pattern=r"\S")
    user_title: str = Field(default="", max_length=80)
    style: str = Field(default="简洁、清晰", min_length=1, max_length=1000, pattern=r"\S")
    timezone: str = "Asia/Shanghai"
    proactive_checks: ProactiveChecksSettings = Field(default_factory=ProactiveChecksSettings)
    proactive_channels: list[Literal["inbox", "feishu"]] = Field(default_factory=default_channels)

    @field_validator("proactive_channels")
    @classmethod
    def valid_proactive_channels(
        cls, value: list[Literal["inbox", "feishu"]]
    ) -> list[Literal["inbox", "feishu"]]:
        """核心通知必选，外部出口只能由本人明确开启。"""
        if "inbox" not in value or len(value) != len(set(value)):
            raise ValueError("必须包含站内信且渠道不能重复")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """设置与日程共用时区规则。"""
        return timezone_name(value)


class MessageInput(StrictModel):
    """服务内部可写角色，HTTP 不直接暴露助手角色入口。"""

    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=50000, pattern=r"\S")
    attachments: list[Identifier] = Field(default_factory=list, max_length=20)
    matter_id: Identifier | None = None


def timezone_name(value: str) -> str:
    """验证时区且返回原名称。"""
    try:
        if value == "Factory":
            raise ValueError("占位时区不能用于个人工作台")
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("时区无效") from exc
    return value


def validate_data(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """选择独立领域模型，未知类型和字段均拒绝。"""
    models: dict[str, type[BaseModel]] = {
        "folder": FolderData,
        "file": FileData,
        "matter": MatterData,
        "todo": TodoData,
        "event": EventData,
        "skill": SkillData,
    }
    if kind not in models:
        raise InvalidRequestError("工作台记录类型无效")
    try:
        return models[kind].model_validate(data).model_dump()
    except ValidationError as exc:
        raise InvalidRequestError(
            "工作台字段无效：" + "; ".join(str(e["msg"]) for e in exc.errors())
        ) from exc


def fingerprint(value: Any) -> str:
    """用稳定摘要保留请求身份，不在审计中写正文。"""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass
class WorkspaceState:
    """单个员工的事务快照；附件字节始终在对象存储。"""

    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    versions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    settings: dict[str, Any] = field(
        default_factory=lambda: {**SettingsData().model_dump(), "version": 1}
    )
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_message_id: int = 1
    messages: list[dict[str, Any]] = field(default_factory=list)
    audits: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    followups: dict[str, dict[str, Any]] = field(default_factory=dict)
    scan_state: dict[str, Any] = field(default_factory=dict)
    pending_notifications: list[dict[str, Any]] = field(default_factory=list)
    pending_reminders: list[dict[str, Any]] = field(default_factory=list)
    pending_reminder_actors: dict[str, str] = field(default_factory=dict)
    pending_reminder_cancellations: list[str] = field(default_factory=list)
    work_tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    work_task_events: list[dict[str, Any]] = field(default_factory=list)
    task_grants: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_dispatch: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_attempts: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_reservations: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_effects: dict[str, dict[str, Any]] = field(default_factory=dict)
    transaction_at: float | None = None
    execution_active: list[dict[str, Any]] = field(default_factory=list)
    tenant_reserved: int = 0
    employee_reserved: int = 0

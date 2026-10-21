"""主动通知在员工事务内的严格校验与来源约束。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.reminders.models import NotificationRecord, default_channels

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState


class PendingNotification(BaseModel):
    """事务 outbox；主动提醒共用既有通知和外部投递流。"""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    notification_key: str = Field(min_length=1, max_length=512)
    source: Literal["proactive_check", "proactive_followup", "task_definition"]
    proactive_check_id: str | None = None
    proactive_followup_id: str | None = None
    task_definition_id: str | None = None
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(max_length=4000)
    due_at: float | None = None
    created_at: float
    alert_after: float
    channels: list[Literal["inbox", "feishu"]] = Field(default_factory=default_channels)

    @model_validator(mode="after")
    def valid_source(self) -> PendingNotification:
        """每条主动通知恰好关联一种对应的业务来源。"""
        sources={'proactive_check':self.proactive_check_id,'proactive_followup':self.proactive_followup_id,
                 'task_definition':self.task_definition_id}
        if sources[self.source] is None or sum(value is not None for value in sources.values())!=1:
            raise ValueError('通知来源和关联记录不一致')
        if "inbox" not in self.channels or len(self.channels) != len(set(self.channels)):
            raise ValueError("必须包含站内信且渠道不能重复")
        return self

    def record(self, tenant: str, user: str, sequence: int) -> NotificationRecord:
        """由存储分配唯一游标，不接受调用者提供身份。"""
        return NotificationRecord(
            notification_id=sequence,
            tenant_id=tenant,
            user_id=user,
            reminder_id=None,
            **self.model_dump(exclude={"channels"}),
        )


def validate_notifications(state: WorkspaceState) -> list[PendingNotification]:
    """先校验完整 outbox，任何非法来源或渠道都回滚整个工作区事务。"""
    try:
        found = [PendingNotification.model_validate(value) for value in state.pending_notifications]
    except ValidationError as exc:
        raise InvalidRequestError("主动通知字段无效") from exc
    enabled_channels = state.settings.get("proactive_channels", ["inbox"])
    for pending in found:
        records = {"proactive_check":state.checks,"proactive_followup":state.followups,"task_definition":state.task_definitions}[pending.source]
        source_id = pending.proactive_check_id or pending.proactive_followup_id or pending.task_definition_id
        if source_id not in records:
            raise InvalidRequestError("主动通知来源不存在或不属于当前员工")
        if any(channel not in enabled_channels for channel in pending.channels):
            raise InvalidRequestError("主动通知渠道尚未由本人启用")
    return found


def validate_proactive(state: WorkspaceState) -> None:
    """内存与数据库一致校验来源归属及同代阶段唯一性。"""
    stages: set[tuple[str, int, str, int]] = set()
    for record in state.checks.values():
        item_id = record.get("item_id")
        if item_id not in state.items or state.items[item_id]["kind"] != "todo":
            raise InvalidRequestError("主动检查只能关联本人的待办")
        generation, stage = record.get("generation"), record.get("stage_index")
        check_type = record.get("check_type")
        if (
            type(generation) is not int
            or generation < 1
            or type(stage) is not int
            or stage < 0
            or check_type not in ("overdue", "stale")
        ):
            raise InvalidRequestError("主动检查代际、类型或阶段无效")
        key = (item_id, generation, check_type, stage)
        if key in stages:
            raise InvalidRequestError("同代检查阶段不能重复")
        stages.add(key)
    stable_keys: set[str] = set()
    for record in state.followups.values():
        if record.get("item_id") is not None and record["item_id"] not in state.items:
            raise InvalidRequestError("主动跟进关联记录不属于当前员工")
        stable_key = record.get("stable_key")
        if stable_key is not None:
            if stable_key in stable_keys:
                raise InvalidRequestError("主动跟进稳定键不能重复")
            stable_keys.add(stable_key)


def invalidated_sources(old: WorkspaceState, state: WorkspaceState) -> list[tuple[str, str]]:
    """业务处理或新通知阶段改变后，旧来源关联的排队投递必须在同一事务撤销。"""
    found = []
    for source, before, after in (
        ("proactive_check", old.checks, state.checks),
        ("proactive_followup", old.followups, state.followups),
    ):
        for identifier, previous in before.items():
            current = after.get(identifier)
            if current == previous:
                continue
            if (
                current is None
                or current.get("status") in ("resolved", "ignored", "dismissed", "waiting")
                or any(
                    current.get(key) != previous.get(key)
                    for key in ("last_notified_at", "due_at", "next_review_at")
                )
            ):
                found.append((source, identifier))
    return found

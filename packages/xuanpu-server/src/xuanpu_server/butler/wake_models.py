"""唤醒事件、待本人处理项与唤醒决定的数据模型（R5-C2）。

- 唤醒事件：匠人一轮结束或需要本人决定时由对账器写入，按 ``(租户, 派工, key)`` 去重；
  消费者处理完逐条确认（ack）。
- 唤醒决定：处理一条事件时先持久化“怎么办”（交付、代答、转问、等审批），再按决定执行
  幂等的副作用；进程在两者之间被终止时，重启后照决定补做，不再跑一遍管家。
- 待本人处理项：转问员工的问题、需要本人审批的操作，以及管家代答的记录（带出处）。
"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003 —— pydantic 在运行时解析字段类型
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EventKind = Literal["finished", "asked"]
PendingKind = Literal["question", "approval", "proxied"]
PendingState = Literal["open", "forwarding", "answered", "resolved", "cancelled"]
SourceKind = Literal["setting", "conversation", "employee"]
WakeAction = Literal["deliver", "forward", "ask_user", "await_approval"]
OPEN_PENDING: frozenset[str] = frozenset({"open", "forwarding"})

_ID = re.compile(r"^[A-Za-z0-9_.:@/-]{1,200}$")
TEXT_MAX = 2000


def _identifier(value: str) -> str:
    """标识只允许有限字符集。"""
    if not _ID.fullmatch(value):
        raise ValueError("标识只能是 1–200 位字母、数字与 _.:@/-")
    return value


class AnswerSource(BaseModel):
    """答复的出处：哪类来源、原文片段。代答时原文必须在来源里逐字出现。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: SourceKind
    quote: str = Field(min_length=1, max_length=TEXT_MAX)
    ref: str | None = Field(default=None, max_length=200)


class WakeDecision(BaseModel):
    """一条唤醒事件的处理决定；一旦写入不再改变（先写者为准）。

    Attributes:
        action: 交付、把答复转给匠人、转问员工、等本人审批。
        passed: 确定性验收是否通过（只对交付有意义）。
        checks: 确定性检查逐项结果 ``{code: ok}``。
        review: 管家模型复核文本，只作「辅助判断」，不改变 ``passed``。
        answer: 转给匠人的答复（代答时逐字取自出处）。
        source: 答复出处。
        question: 转问员工的问题。
        artisan_text: 匠人这一轮的回复摘录（交付或转问时给员工看）。
        final_state: 交付时派工的终态（completed / failed / cancelled）。
        end_reason: 交付时的结束原因代码。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    action: WakeAction
    passed: bool = False
    checks: dict[str, bool] = Field(default_factory=dict)
    review: str | None = Field(default=None, max_length=TEXT_MAX)
    answer: str | None = Field(default=None, max_length=TEXT_MAX)
    source: AnswerSource | None = None
    question: str | None = Field(default=None, max_length=TEXT_MAX)
    artisan_text: str | None = Field(default=None, max_length=8000)
    final_state: Literal["completed", "failed", "cancelled"] | None = None
    end_reason: str | None = Field(default=None, max_length=64)


class ButlerEvent(BaseModel):
    """一条唤醒事件。``key`` 标识宿主侧的一个可观察事实（一轮结束、一次状态变化）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    event_id: str
    tenant_id: str
    user_id: str
    dispatch_id: str
    key: str
    kind: EventKind
    created_at: datetime
    decision: WakeDecision | None = None
    acked_at: datetime | None = None

    @field_validator("event_id", "tenant_id", "user_id", "dispatch_id", "key")
    @classmethod
    def _ids(cls, value: str) -> str:
        """标识校验。"""
        return _identifier(value)


class PendingItem(BaseModel):
    """一件待本人处理的事（或一条代答记录）。改动须带期望版本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    pending_id: str
    tenant_id: str
    user_id: str
    dispatch_id: str
    source_key: str
    kind: PendingKind
    question: str = Field(min_length=1, max_length=TEXT_MAX)
    host: Literal["task", "session"]
    host_ref: str
    state: PendingState = "open"
    answer: str | None = Field(default=None, max_length=TEXT_MAX)
    answer_source: AnswerSource | None = None
    answer_request_id: str | None = Field(default=None, max_length=200)
    version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("pending_id", "tenant_id", "user_id", "dispatch_id", "source_key", "host_ref")
    @classmethod
    def _ids(cls, value: str) -> str:
        """标识校验。"""
        return _identifier(value)

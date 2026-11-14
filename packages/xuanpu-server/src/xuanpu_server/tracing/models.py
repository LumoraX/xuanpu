"""trace 与 span 的数据模型：只含元数据，构造时拒绝超长与控制字符。"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003 —— pydantic 在运行时解析字段类型
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Iterable

TraceOrigin = Literal["user", "schedule", "event"]
SpanKind = Literal["butler", "dispatch", "group", "artisan", "tool", "wait"]
SpanState = Literal[
    "queued", "running", "waiting_children", "waiting_input",
    "completed", "failed", "cancelled", "unknown",
]
TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled", "unknown"})

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
NAME_MAX = 120


def _identifier(value: str) -> str:
    """标识只允许有限字符集，防止把正文或路径塞进标识字段。"""
    if not _ID.fullmatch(value):
        raise ValueError("标识只能是 1–160 位字母、数字与 _.:-")
    return value


def display_name(value: str) -> str:
    """显示名去掉控制字符并截到上限；只放技能、工具或匠人名，不放参数。"""
    cleaned = _CONTROL.sub("", value).strip()
    if not cleaned:
        raise ValueError("显示名不能为空")
    return cleaned[:NAME_MAX]


class TraceOwner(BaseModel):
    """trace 的归属：租户与员工。存储层只认它，鉴权在接入层完成。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: str
    user_id: str

    @field_validator("tenant_id", "user_id")
    @classmethod
    def _check(cls, value: str) -> str:
        """归属字段同样走标识校验。"""
        return _identifier(value)


class AttemptUsage(BaseModel):
    """一次尝试自身的用量绝对值；同一尝试的新快照覆盖旧值，不累加。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    attempt_id: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    usage_complete: bool = False

    @field_validator("attempt_id")
    @classmethod
    def _check(cls, value: str) -> str:
        """尝试标识走标识校验。"""
        return _identifier(value)


class SpanFact(BaseModel):
    """采集器交给存储的一条事实。``seq`` 是该 trace 内单调递增的事实序号。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    seq: int = Field(ge=1)
    span_id: str
    at: datetime
    kind: SpanKind | None = None
    name: str | None = None
    parent_span_id: str | None = None
    ref: str | None = None
    attempt_id: str | None = None
    state: SpanState | None = None
    usage: AttemptUsage | None = None
    end_reason: str | None = None

    @field_validator("span_id", "parent_span_id", "ref", "attempt_id")
    @classmethod
    def _ids(cls, value: str | None) -> str | None:
        """可空标识非空时校验。"""
        return None if value is None else _identifier(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        """显示名清洗。"""
        return None if value is None else display_name(value)

    @field_validator("end_reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        """结束原因只能是代码，不能是自由文本。"""
        if value is not None and not _CODE.fullmatch(value):
            raise ValueError("结束原因必须是小写代码")
        return value


class SpanRecord(BaseModel):
    """一个 span 的当前状态。``attempts`` 按首次出现顺序保存每次尝试的用量。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    span_id: str
    kind: SpanKind
    name: str
    parent_span_id: str | None = None
    ref: str | None = None
    state: SpanState = "queued"
    started_at: datetime
    ended_at: datetime | None = None
    current_attempt_id: str | None = None
    attempts: tuple[AttemptUsage, ...] = ()
    end_reason: str | None = None
    incomplete: bool = False

    @property
    def input_tokens(self) -> int | None:
        """各尝试自身输入用量之和；尚无用量或任一尝试未知则为 None，不用 0 冒充。"""
        return _sum(a.input_tokens for a in self.attempts)

    @property
    def output_tokens(self) -> int | None:
        """各尝试自身输出用量之和；规则同上。"""
        return _sum(a.output_tokens for a in self.attempts)

    @property
    def usage_complete(self) -> bool:
        """至少一次尝试且每次尝试的用量都完整。"""
        return bool(self.attempts) and all(a.usage_complete for a in self.attempts)


class TraceRecord(BaseModel):
    """一条 trace 的头信息。``last_seq`` 是已应用的最大事实序号。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    trace_id: str
    owner: TraceOwner
    origin: TraceOrigin
    title: str
    created_at: datetime
    updated_at: datetime
    last_seq: int = 0
    incomplete: bool = False
    truncated: bool = False

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        """标题按显示名清洗；它来自管家对请求的概括，不是原文。"""
        return display_name(value)


class TraceView(BaseModel):
    """一条 trace 的完整读取结果：头信息加全部 span。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    trace: TraceRecord
    spans: tuple[SpanRecord, ...]

    @property
    def active(self) -> bool:
        """仍有未到终态的 span。"""
        return any(span.state not in TERMINAL_STATES for span in self.spans)


def _sum(values: Iterable[int | None]) -> int | None:
    """全部已知才求和；一个都没有或任一未知时返回 None，不用 0 冒充未知。"""
    total: int | None = None
    for value in values:
        if value is None:
            return None
        total = (total or 0) + value
    return total

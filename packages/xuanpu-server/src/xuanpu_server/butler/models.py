"""匠人与派工记录的数据模型，以及派工状态机（纯函数）。

派工记录只存管家交给匠人的任务说明与材料引用；匠人的正文与结果仍在其任务或会话里，
按原权限读取。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime  # noqa: TC003 —— pydantic 在运行时解析字段类型
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xuanpu_server.errors import ConflictError

HostKind = Literal["task", "session"]
DispatchState = Literal[
    "accepted", "running", "asking", "completed", "failed", "cancelled", "delivered",
]
FINISHED_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
CLOSED_STATES: frozenset[str] = FINISHED_STATES | {"delivered"}

# 允许的转换：键是当前状态，值是可去的状态。终态只能交付（验收后）。
_TRANSITIONS: dict[str, frozenset[str]] = {
    "accepted": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"asking", "completed", "failed", "cancelled"}),
    "asking": frozenset({"running", "completed", "failed", "cancelled"}),
    "completed": frozenset({"delivered"}),
    "failed": frozenset({"delivered"}),
    "cancelled": frozenset({"delivered"}),
    "delivered": frozenset(),
}

_ID = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
INSTRUCTION_MAX = 4000


def _identifier(value: str) -> str:
    """标识只允许有限字符集，防止把正文塞进引用字段。"""
    if not _ID.fullmatch(value):
        raise ValueError("标识只能是 1–160 位字母、数字与 _.:@/-")
    return value


class ArtisanRef(BaseModel):
    """派工时冻结的匠人引用：包、版本、摘要、入口与授权决策引用。

    ``decision_ref`` 记录本次可用性是依据什么判定的（现在是租户生效目录，R4-B 后是
    到人能力决策），供事后核对，不作为再次授权的依据。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    package: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    digest: str = Field(min_length=1, max_length=128)
    entry: str = Field(min_length=1, max_length=128)
    decision_ref: str

    @field_validator("decision_ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        """决策引用走标识校验。"""
        return _identifier(value)


class Artisan(BaseModel):
    """本人此刻可用的一个匠人：给管家看的说明加派工时冻结的引用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    ref: ArtisanRef
    description: str = Field(max_length=2000)
    entry_description: str = Field(max_length=2000)
    host: HostKind


class DispatchRecord(BaseModel):
    """一次派工。``version`` 每次状态变化加一，改动须带期望版本。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    dispatch_id: str
    tenant_id: str
    user_id: str
    request_id: str = Field(min_length=1, max_length=128)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_id: str
    parent_span_id: str | None = None
    artisan: ArtisanRef
    host: HostKind
    host_ref: str | None = None
    instruction: str = Field(min_length=1, max_length=INSTRUCTION_MAX)
    materials: tuple[str, ...] = Field(default=(), max_length=8)
    state: DispatchState = "accepted"
    end_reason: str | None = None
    version: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("dispatch_id", "tenant_id", "user_id", "trace_id", "parent_span_id",
                     "host_ref")
    @classmethod
    def _ids(cls, value: str | None) -> str | None:
        """可空标识非空时校验。"""
        return None if value is None else _identifier(value)

    @field_validator("materials")
    @classmethod
    def _materials(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """材料只是文件引用，不能重复。"""
        if len(set(value)) != len(value):
            raise ValueError("同一材料不能重复")
        return tuple(_identifier(item) for item in value)

    @field_validator("end_reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        """结束原因只能是代码，不能是自由文本。"""
        if value is not None and not _CODE.fullmatch(value):
            raise ValueError("结束原因必须是小写代码")
        return value

    @property
    def active(self) -> bool:
        """匠人还没到终态。"""
        return self.state not in CLOSED_STATES


def dispatch_fingerprint(
    *, trace_id: str, artisan: ArtisanRef, instruction: str, materials: tuple[str, ...],
) -> str:
    """同一 ``request_id`` 的派工内容指纹：同键同体回原记录，不同体冲突。"""
    body = json.dumps(
        {"trace_id": trace_id, "package": artisan.package, "entry": artisan.entry,
         "instruction": instruction, "materials": list(materials)},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def transition(
    record: DispatchRecord, to: DispatchState, *, at: datetime,
    host_ref: str | None = None, end_reason: str | None = None,
) -> DispatchRecord:
    """按状态机推进一步；不允许的转换报冲突，不悄悄忽略。

    ``host_ref`` 只能在首次进入 running 时绑定一次；之后给出不同值视为冲突。
    """
    if to not in _TRANSITIONS[record.state]:
        raise ConflictError(f"派工不能从 {record.state} 变为 {to}")
    if host_ref is not None and record.host_ref not in (None, host_ref):
        raise ConflictError("派工已绑定到另一个执行")
    if to == "running" and (host_ref or record.host_ref) is None:
        raise ConflictError("派工开始运行时必须绑定任务或会话")
    # 经 model_validate 重建，新字段同样过校验（model_copy 不校验）
    return DispatchRecord.model_validate({
        **record.model_dump(), "state": to, "updated_at": at, "version": record.version + 1,
        "host_ref": host_ref or record.host_ref,
        "end_reason": end_reason if end_reason is not None else record.end_reason,
    })

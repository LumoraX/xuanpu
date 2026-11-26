"""双层本地 Transcript 与全局索引的数据模型（ADR 0026）。"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003 —— pydantic 运行时解析
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TranscriptKind = Literal["butler", "artisan", "work_task", "forge", "session"]
TranscriptMode = Literal["compact", "full"]
TranscriptStatus = Literal["running", "completed", "failed", "cancelled", "suspended"]
ReadSource = Literal["local", "remote_node", "object_store"]

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")


def _safe_id(value: str) -> str:
    """标识只允许受控字符集，防止路径穿越或注入正文。"""
    if not _SAFE_ID.fullmatch(value) or ".." in value:
        raise ValueError(f"非法 Transcript 标识：{value!r}")
    return value


class TranscriptStep(BaseModel):
    """单步 Transcript 记录（compact 与 full 行号一一对应）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    step_index: int = Field(ge=0)
    ts: float
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    kind: str = Field(min_length=1, max_length=64)
    status: str = Field(default="ok", min_length=1, max_length=32)
    duration_ms: int | None = Field(default=None, ge=0)
    usage: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    truncated_fields: tuple[str, ...] = ()


class TranscriptIndexRecord(BaseModel):
    """PostgreSQL 全局元数据索引行（绝不存大正文）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: str
    span_id: str
    trace_id: str
    parent_span_id: str | None = None
    session_id: str | None = None
    dispatch_id: str | None = None
    user_id: str | None = None
    kind: TranscriptKind
    node_id: str = Field(min_length=1, max_length=128)
    node_addr: str | None = Field(default=None, max_length=256)
    local_relpath: str = Field(min_length=1, max_length=512)
    compact_object_key: str | None = Field(default=None, max_length=512)
    full_object_key: str | None = Field(default=None, max_length=512)
    byte_size: int = Field(default=0, ge=0)
    line_count: int = Field(default=0, ge=0)
    status: TranscriptStatus = "running"
    error_code: str | None = Field(default=None, max_length=128)
    created_at: datetime
    archived_at: datetime | None = None

    @field_validator("tenant_id", "span_id", "trace_id")
    @classmethod
    def _valid_ids(cls, value: str) -> str:
        """校验租户、trace 与 span 标识。"""
        return _safe_id(value)


class TranscriptSlice(BaseModel):
    """按行号切片回读结果，标明实际命中的三态来源。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    trace_id: str
    span_id: str
    mode: TranscriptMode
    source: ReadSource
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=0)
    total_lines: int = Field(ge=0)
    lines: tuple[dict[str, Any], ...] = ()

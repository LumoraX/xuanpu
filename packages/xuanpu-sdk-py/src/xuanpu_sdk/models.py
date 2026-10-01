"""客户端的数据类型：对平台响应的直接映射。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TERMINAL_KINDS: frozenset[str] = frozenset({"turn_completed", "turn_failed", "turn_suspended"})
# 送回审批被拒或只核销了一部分：没有续跑，这条事件就是结束（与平台口径一致）
RESUME_ENDING_KINDS: frozenset[str] = frozenset(
    {"suspension_resolve_rejected", "suspension_partially_resolved"}
)


@dataclass(frozen=True)
class Session:
    """一个会话。"""

    session_id: str
    package: str
    version: str
    entry: str
    status: str
    owner: str | None
    created_at: float
    updated_at: float
    created_by: str
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Session:
        """由响应构造。"""
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass(frozen=True)
class Event:
    """事件流里的一条内核事件。"""

    kind: str
    submission_id: str
    seq: int
    at: float
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        """是否结束了这一轮对话。"""
        if self.kind == "turn_suspended" or self.kind in RESUME_ENDING_KINDS:
            return True
        return self.kind in TERMINAL_KINDS and bool(self.data.get("is_root", False))

    @property
    def text_delta(self) -> str:
        """流式文本增量；不是文本事件时为空串。"""
        delta = self.data.get("delta") if self.kind == "assistant_text" else None
        return delta if isinstance(delta, str) else ""


@dataclass(frozen=True)
class PackageVersion:
    """业务包的一个版本。"""

    name: str
    version: str
    digest: str
    active: bool
    published_at: float
    published_by: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PackageVersion:
        """由响应构造。"""
        return cls(**{key: data[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class AuditRecord:
    """一条审计记录。"""

    audit_id: int
    source: str
    kind: str
    at: float
    actor: str
    session_id: str | None
    stream: str | None
    seq: int | None
    data: dict[str, Any]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AuditRecord:
        """由响应构造。"""
        return cls(**{key: data[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Usage:
    """某一天的用量。"""

    day: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    turns: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Usage:
        """由响应构造。"""
        return cls(**{key: data[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class IssuedKey:
    """刚签发的 API key。``key`` 只在这里出现一次。"""

    key: str
    key_id: str
    tenant_id: str
    name: str
    role: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> IssuedKey:
        """由响应构造。"""
        return cls(**{key: data[key] for key in cls.__dataclass_fields__})


__all__ = [
    "RESUME_ENDING_KINDS",
    "TERMINAL_KINDS",
    "AuditRecord",
    "Event",
    "IssuedKey",
    "PackageVersion",
    "Session",
    "Usage",
]

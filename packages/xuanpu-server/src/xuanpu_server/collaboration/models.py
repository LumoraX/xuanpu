"""交接、组织决定和来源动态的类型化协调快照。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.approvals.models import (  # noqa: TC001 —— Pydantic嵌套模型
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
)
from xuanpu_server.handoffs.models import (  # noqa: TC001 —— Pydantic嵌套模型
    Delivery,
    Handoff,
    HandoffEvent,
)
from xuanpu_server.shared_matters.models import SharedRecord


class CollaborationConfig(BaseModel):
    """到人和部门共用显式灰度，默认禁止新受理。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    enabled: bool = False
    tenants: tuple[str, ...] = ()
    max_active: int = Field(default=500, ge=1, le=5000)

    def allows(self, tenant: str) -> bool:
        """空名单不表示全量开放。"""
        return self.enabled and tenant in self.tenants


class CollaborationNotification(SharedRecord):
    """站内通知只存固定来源编号，正文和人数每次使用当前权限计算。"""
    user_id: str
    user_epoch: int
    sequence: int
    handoff_id: str
    approval_id: str | None = None
    source_kind: Literal['handoff', 'approval_request']
    event_id: str
    semantic_key: str
    read_at: float | None = None


class CollaborationState(BaseModel):
    """与组织及共享快照一起提交，禁止交接成功但决定或通知丢失。"""
    handoffs: dict[str, Handoff] = Field(default_factory=dict)
    events: dict[str, HandoffEvent] = Field(default_factory=dict)
    deliveries: dict[str, Delivery] = Field(default_factory=dict)
    policies: dict[str, ApprovalPolicy] = Field(default_factory=dict)
    approvals: dict[str, ApprovalRequest] = Field(default_factory=dict)
    decisions: dict[str, ApprovalDecision] = Field(default_factory=dict)
    notifications: dict[str, CollaborationNotification] = Field(default_factory=dict)

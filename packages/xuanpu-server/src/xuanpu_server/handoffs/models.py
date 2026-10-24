"""交接保存固定输入、真人责任与独立交付历史。"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from xuanpu_server.handoffs.schemas import ResultRef  # noqa: TC001 —— Pydantic运行时嵌套模型
from xuanpu_server.shared_matters.models import SharedRecord

HandoffStatus = Literal['submitted', 'accepted', 'in_progress', 'needs_information', 'delivered',
    'signed_off', 'returned', 'cancelled', 'disputed']


class Handoff(SharedRecord):
    """目标到人时不要求部门，但绑定当前员工身份代次。"""
    shared_id: str
    requester_id: str
    requester_epoch: int
    target_kind: Literal['user', 'unit']
    target_id: str
    target_epoch: int | None = None
    title: str = Field(max_length=500)
    summary: str = Field(max_length=4000)
    content_snapshot: str | None = None
    material_revision_ids: tuple[str, ...] = ()
    source_access_epoch: int
    source_grant_id: str
    source_grant_epoch: int
    input_digest: str
    status: HandoffStatus = 'submitted'
    due_at: float | None = None
    policy_id: str | None = None
    policy_version: int | None = None
    assignee_id: str | None = None
    assignee_epoch: int | None = None
    assignee_authority_digest: str | None = None
    assignee_grant_id: str | None = None
    cancel_requested: bool = False
    blocked_reason: str | None = None
    delivery_id: str | None = None
    delivery_version: int = 0


class HandoffEvent(SharedRecord):
    """明确动作及原责任历史追加保存，不改写旧记录。"""
    handoff_id: str
    actor_id: str | None = None
    action: str
    previous_status: str
    next_status: str
    assignee_id: str | None = None
    reason: str = Field(default='', max_length=4000)
    input_digest: str = ''
    material_revision_ids: tuple[str, ...] = ()
    summary: str = ''
    content_snapshot: str | None = None


class Delivery(SharedRecord):
    """固定成果与合法审批独立于签收记录。"""
    handoff_id: str
    assignee_id: str
    input_digest: str
    delivery_version: int
    result_refs: tuple[ResultRef, ...]
    approval_id: str | None = None

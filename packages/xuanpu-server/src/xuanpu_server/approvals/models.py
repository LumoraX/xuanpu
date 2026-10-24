"""组织审批冻结策略修订、具体员工、身份足迹及目标摘要。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from xuanpu_server.approvals.schemas import StepSelector  # noqa: TC001 —— Pydantic嵌套模型
from xuanpu_server.handoffs.schemas import ResultRef  # noqa: TC001 —— Pydantic嵌套模型
from xuanpu_server.shared_matters.models import SharedRecord


class FrozenSelector(StepSelector):
    """管理员确认规则时即固定明确员工身份代次。"""
    user_epoch: int | None = None


class PolicyRevision(BaseModel):
    """旧策略修订不可改变，新策略不追改已发生的合法决定。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: int
    name: str
    steps: tuple[FrozenSelector, ...]


class ApprovalPolicy(SharedRecord):
    """当前规则与全部历史修订同事务保存。"""
    name: str
    status: Literal['active', 'disabled'] = 'active'
    revisions: tuple[PolicyRevision, ...]


class ApprovalStep(BaseModel):
    """解析后每步唯一真人，部门足迹与员工身份同样冻结。"""
    model_config = ConfigDict(extra='forbid', frozen=True)
    index: int
    user_id: str
    user_epoch: int
    selector: FrozenSelector
    authority_digest: str
    grant_id: str | None = None


class ApprovalRequest(SharedRecord):
    """未决职责失效永久阻塞，新输入需要新申请。"""
    policy_id: str
    policy_version: int
    applicant_id: str
    applicant_epoch: int
    handoff_id: str
    target_version: int
    target_digest: str
    result_refs: tuple[ResultRef, ...]
    steps: tuple[ApprovalStep, ...]
    current_step: int = 0
    status: Literal['pending', 'approved', 'rejected', 'returned', 'withdrawn', 'blocked', 'superseded'] = 'pending'
    blocked_reason: str | None = None


class ApprovalDecision(SharedRecord):
    """每步只保存一次明确组织决定，回执不授权其他版本目标。"""
    approval_id: str
    step_index: int
    actor_id: str
    decision: Literal['approve', 'reject', 'return', 'withdraw']
    target_digest: str
    reason: str = ''

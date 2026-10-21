"""持久执行授权、派发、租约与检查点的严格领域契约。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from xuanpu_server.tenancy import Principal, TenantRole
from xuanpu_server.work_tasks.models import Digest, FrozenModel, Identifier, Timestamp


class ExecutionGrant(FrozenModel):
    """员工明确交办形成的独立授权，跨重试共享次数和预算。"""

    grant_id: Identifier
    tenant_id: Identifier
    user_id: Identifier
    actor: str
    role: TenantRole
    key_id: str | None = None
    package_digest: Digest
    input_digests: tuple[str, ...] = ()
    created_at: Timestamp
    expires_at: Timestamp
    revoked: bool = False
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_tokens: int = Field(default=300000, ge=1)
    attempts_used: int = Field(default=0, ge=0)

    def principal(self) -> Principal:
        """恢复原始调用者，绝不提升到系统管理员。"""
        return Principal(role=self.role, tenant_id=self.tenant_id,
                         user_id=self.user_id, key_id=self.key_id)


class Checkpoint(FrozenModel):
    """有界阶段证据；只允许平台产生的结构化内容。"""

    phase: str = Field(default="accepted", max_length=64)
    evidence: dict[str, Any] = Field(default_factory=dict, max_length=256)
    at: Timestamp


class DispatchRecord(FrozenModel):
    """唯一持久派发责任；不改变原有业务终态集合。"""

    task_id: Identifier
    grant_id: Identifier
    state: Literal['ready', 'leased', 'retry_wait', 'blocked', 'waiting_user',
                   'paused_shutdown', 'settled'] = 'ready'
    attempt_no: int = Field(default=0, ge=0)
    epoch: int = Field(default=0, ge=0)
    owner: str | None = None
    expires_at: Timestamp = 0.0
    due_at: Timestamp
    block_reason: str | None = None
    checkpoint: Checkpoint


class TaskAttempt(FrozenModel):
    """每次执行独立的隔离范围与会话；结束后仍保留责任证据。"""

    task_id: Identifier
    attempt_no: int = Field(ge=1)
    owner: Identifier
    epoch: int = Field(ge=1)
    started_at: Timestamp
    expires_at: Timestamp
    finished_at: Timestamp | None = None
    session_id: Identifier | None = None
    scope_id: str = Field(max_length=300)
    phase: str = 'claimed'
    error_code: str | None = None
    result_receipt: dict[str, Any] = Field(default_factory=dict,max_length=256)
    resume_evidence: tuple[dict[str, Any], ...] = Field(default=(),max_length=128)


@dataclass(frozen=True)
class AttemptLease:
    """仅平台内部传递的执行所有权，不能由客户端构造。"""

    tenant_id: str
    user_id: str
    task_id: str
    attempt_no: int
    owner: str
    epoch: int
    expires_at: float


class BudgetReservation(FrozenModel):
    """实际模型请求的保守上界；未知用量永不自动退款。"""

    reservation_id: Identifier
    task_id: Identifier
    grant_id: Identifier
    attempt_no: int
    day: str
    upper_bound: int = Field(ge=1)
    state: Literal['held', 'settled'] = 'held'
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    at: Timestamp

    @property
    def charged(self) -> int:
        """未知请求按完整上界占用额度，已核实按真实用量计。"""
        return self.upper_bound if self.state == 'held' else self.input_tokens + self.output_tokens

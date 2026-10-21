"""恢复只依据持久事实选择安全阶段，不猜测外部执行结果。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .execution_models import DispatchRecord
    from .models import WorkTaskRecord

RecoveryDecision = Literal['keep_terminal', 'cleanup', 'wait_user', 'start', 'recompute',
    'commit_artifacts', 'resume_proven_barrier', 'block_unknown', 'block_auth', 'block_budget',
    'block_resume', 'fail_attempts']


@dataclass(frozen=True)
class RecoveryFacts:
    """所有事实来自权威存储或已确认清理结果。"""

    terminal: bool = False
    resources_pending: bool = False
    grant_valid: bool = True
    unknown_effect: bool = False
    any_effect: bool = False
    frozen: bool = False
    waiting: bool = False
    pending_http_safe: bool = False
    attempts_exhausted: bool = False
    budget_available: bool = True


def choose_recovery(facts: RecoveryFacts) -> RecoveryDecision:
    """未知写入与失效授权优先，冻结上传不依赖模型预算。"""
    if facts.terminal:
        return 'keep_terminal'
    if facts.resources_pending:
        return 'cleanup'
    if not facts.grant_valid:
        return 'block_auth'
    if facts.unknown_effect:
        return 'block_unknown'
    if facts.frozen:
        return 'commit_artifacts'
    if facts.waiting:
        return 'resume_proven_barrier' if facts.pending_http_safe else 'block_resume'
    if facts.any_effect:
        return 'block_resume'
    if facts.attempts_exhausted:
        return 'fail_attempts'
    return 'recompute' if facts.budget_available else 'block_budget'


def dispatch_plan(task: WorkTaskRecord, dispatch: DispatchRecord, choice: RecoveryDecision, *,
                  auth_reason: str, now: float, restart: bool, started_at: float | None) -> tuple[str,str | None,float]:
    """纯阶段到派发映射，持久退避不会因重启清零。"""
    reason: str | None = None
    if task.status == 'waiting_user' and task.outputs_verified:
        target = 'waiting_user'
    elif choice in {'keep_terminal','fail_attempts'}:
        target,reason = 'settled',choice
    elif choice in {'start','recompute','commit_artifacts'}:
        target = 'ready'
    elif choice == 'resume_proven_barrier':
        target = 'ready' if task.pending_decision else 'waiting_user'
    else:
        target,reason = 'blocked',auth_reason if choice == 'block_auth' else choice
    due_at = max(now,dispatch.due_at) if dispatch.state == 'retry_wait' else now
    if restart and dispatch.state == 'leased' and choice == 'recompute' and started_at is not None:
        due_at = max(due_at,started_at + (30. if dispatch.attempt_no == 1 else 120.))
        target = 'retry_wait' if due_at > now else 'ready'
    return target,reason,due_at

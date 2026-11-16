"""匠人结果的确定性验收（R5-C2）。

先把宿主（持久任务或会话的一轮）读成 ``HostSnapshot``，再逐项检查：
到达真实终态、结果是成功、声明的成果存在且可读、没有未决确认、没有结果未知。
检查是纯函数；模型复核只作「辅助判断」，不参与 ``passed``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.errors import InvalidRequestError, NotFoundError
from xuanpu_server.runtime.events import RESUME_ENDING_KINDS, TERMINAL_KINDS
from xuanpu_server.runtime.operation_status import operation_outcome
from xuanpu_server.work_tasks.models import TERMINAL as TASK_TERMINAL

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal

_ENDING_KINDS = TERMINAL_KINDS | RESUME_ENDING_KINDS | {"suspension_resolved"}
CHECKS = ("terminal", "succeeded", "outputs", "no_pending", "no_unknown")
SUCCESS = frozenset({"completed", "succeeded"})
TEXT_MAX = 8000


@dataclass(frozen=True)
class HostSnapshot:
    """宿主某一刻的可观察事实。

    Attributes:
        host: 承载方式。
        terminal: 是否到达真实终态（会话：这一轮结束；任务：任务终态）。
        outcome: 结果分类（completed / succeeded / failed / cancelled / suspended /
            outcome_unknown / interrupted / running / waiting_user）。
        result_text: 匠人给出的结果文本（会话：这一轮回复全文；任务：结果摘要）。
        outputs: 声明的成果 ``(名称, 是否可读)``。
        pending_confirmations: 未决的确认数。
    """

    host: Literal["task", "session"]
    terminal: bool
    outcome: str
    result_text: str = ""
    outputs: tuple[tuple[str, bool], ...] = field(default=())
    pending_confirmations: int = 0


@dataclass(frozen=True)
class Inspection:
    """验收结果：逐项检查与总结论。"""

    checks: dict[str, bool]
    passed: bool
    final_state: Literal["completed", "failed", "cancelled"]
    end_reason: str


def inspect(snapshot: HostSnapshot) -> Inspection:
    """逐项检查；任一不过即不通过，失败不改写为成功。"""
    checks = {
        "terminal": snapshot.terminal,
        "succeeded": snapshot.outcome in SUCCESS,
        "outputs": all(ok for _, ok in snapshot.outputs)
        and (bool(snapshot.outputs) or bool(snapshot.result_text.strip())),
        "no_pending": snapshot.pending_confirmations == 0,
        "no_unknown": snapshot.outcome != "outcome_unknown",
    }
    passed = all(checks.values())
    if passed:
        return Inspection(checks, True, "completed", "ok")
    if snapshot.outcome == "cancelled":
        return Inspection(checks, False, "cancelled", "cancelled")
    failed = next(code for code in CHECKS if not checks[code])
    return Inspection(checks, False, "failed", "check_" + failed)


async def task_snapshot(platform: Platform, who: Principal, task_id: str) -> HostSnapshot:
    """读持久任务：状态、结果摘要、成果是否可读、未决确认。"""
    task = await platform.work_tasks.store.get(who, task_id)
    outputs = []
    for artifact in task.artifacts:
        outputs.append((artifact.name, await _readable(platform, who, artifact.item_id,
                                                      artifact.revision)))
    return HostSnapshot(
        host="task", terminal=task.status in TASK_TERMINAL, outcome=task.status,
        result_text=task.result_summary[:TEXT_MAX], outputs=tuple(outputs),
        pending_confirmations=len(task.pending))


async def _readable(platform: Platform, who: Principal, item_id: str, revision: int) -> bool:
    """成果在本人空间里存在且能读出。"""
    try:
        content, _, _ = await platform.workspace.download(who, item_id, revision)
    except (NotFoundError, InvalidRequestError):
        return False
    return bool(content)


async def session_round(
    platform: Platform, tenant_id: str, session_id: str, submission_id: str, text: str,
) -> HostSnapshot:
    """读会话的一轮（一个提交）：找该提交的终态审计记录，按统一口径分类。"""
    records = await platform.store.query_audit(
        tenant_id, AuditQuery(session_id=session_id, submission_id=submission_id, limit=1000))
    # 只认结束一轮的内核事件；回复全文、用量估算等随终态写的记录不是分类依据
    terminal = [r for r in records
                if r.data.get("operation_terminal") is True and r.kind in _ENDING_KINDS]
    if not terminal:
        return HostSnapshot(host="session", terminal=False, outcome="running", result_text=text)
    last = terminal[-1]
    outcome = operation_outcome(last.kind, last.data)
    pending = len(last.data.get("pending") or []) if outcome in ("suspended", "outcome_unknown") else 0
    if outcome == "suspended" and pending == 0:
        pending = 1
    return HostSnapshot(host="session", terminal=outcome not in ("suspended",),
                        outcome=outcome, result_text=text[:TEXT_MAX],
                        pending_confirmations=pending)

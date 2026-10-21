"""把内核核销事件与持久审批回执绑定，保留原拒绝补记。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.models import AuditRecord

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from xuanpu_server.controlplane.models import SessionRecord
    from xuanpu_server.runtime.receipt_store import ReceiptStore
    from xuanpu_server.runtime.resolutions import ResolutionWatch


@dataclass(frozen=True)
class ReplayedTurn:
    """返回原提交的审计视图，不能重新提交同一个审批。"""

    session_id: str
    submission_id: str
    events: list[dict[str, Any]]
    replayed: bool = True


class ReceiptWatch:
    """结论在关闭期间也必须被跟踪和等待。"""

    def __init__(self, store: ReceiptStore, record: SessionRecord, operation: str,
                 submission: str, delegate: ResolutionWatch | None,
                 track: Callable[[Awaitable[None]], None], now: Callable[[], float],
                 expected: frozenset[str]) -> None:
        """绑定实际 submission，核销事件先到也不会丢失标识。"""
        self._store, self._record = store, record
        self._operation, self._submission = operation, submission
        self._delegate, self._track, self._now = delegate, track, now
        self._expected = expected

    async def _settle(self, state: str) -> None:
        """先保存已受理提交，再原子提交核销与审计。"""
        at = self._now()
        await self._store.submitted(self._record.tenant_id, self._operation, self._submission, at=at)
        audit = AuditRecord(self._record.tenant_id, "control", "operation_settled", at,
            "system:approval-receipts", session_id=self._record.session_id,
            user_id=self._record.user_id,
            data={"operation_id": self._operation, "submission_id": self._submission, "state": state})
        await self._store.settle(self._record.tenant_id, self._operation, state, audit, at=at)

    async def resolved(self, request_ids: frozenset[str]) -> None:
        """核销表示决定已应用，不宣称外部操作完成。"""
        if request_ids != self._expected:
            await self._settle("outcome_unknown")
            if self._delegate is not None:
                self._delegate.abandoned()
            return
        await self._settle("applied")
        if self._delegate is not None:
            await self._delegate.resolved(request_ids)

    async def refused(self, reason: str) -> None:
        """内核证明整次没有核销，记录拒绝后允许用户修正答案。"""
        await self._settle("rejected")
        await self._store.release(self._record.tenant_id, self._operation)
        if self._delegate is not None:
            work = self._delegate.refused(reason)
            if work is not None:
                await work

    def abandoned(self) -> None:
        """无核销结论时禁止以超时自动再执行。"""
        self._track(self._settle("outcome_unknown"))
        if self._delegate is not None:
            self._delegate.abandoned()

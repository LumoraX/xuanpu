"""会话的历史视图：只读审计推算出来的东西（还挂着的请求、一轮对话的事件回放）。

审计在控制面里、各实例共享，所以这些读取不要求本实例持有会话。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.models import AuditQuery

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import AuditRecord
    from xuanpu_server.controlplane.store import ControlPlaneStore

_PAGE = 1000

_SETTLING_KINDS = frozenset(
    {"suspension_resolved", "suspension_partially_resolved", "suspension_expired"}
)


class _PendingTracker:
    """顺着审计记录推算当前还挂着的请求。"""

    def __init__(self) -> None:
        """从没有挂起开始。"""
        self._suspended: AuditRecord | None = None
        self._settled: set[str] = set()

    def observe(self, item: AuditRecord) -> None:
        """看一条引擎事件。"""
        if item.kind == "turn_suspended":
            self._suspended, self._settled = item, set()
            return
        if self._suspended is None or item.kind not in _SETTLING_KINDS:
            return
        if item.data.get("record_id") != self._suspended.data.get("record_id"):
            return
        resolved = item.data.get("resolved_request_ids") or item.data.get("request_ids")
        if item.kind == "suspension_expired" or not isinstance(resolved, list):
            self._suspended = None
        else:
            self._settled.update(str(value) for value in resolved)

    def remaining(self) -> list[dict[str, Any]]:
        """还没处理的请求。"""
        if self._suspended is None:
            return []
        pending = self._suspended.data.get("pending")
        if not isinstance(pending, list):
            return []
        return [
            dict(item)
            for item in pending
            if isinstance(item, dict) and str(item.get("request_id")) not in self._settled
        ]

    def snapshot(self) -> PendingSuspension:
        """还没处理的请求，连同挂起的那一轮的提交标识。"""
        if self._suspended is None:
            return PendingSuspension(submission_id=None, requests=[])
        # 引擎事件的审计记录都带 submission_id（见 events 模块的 _record）
        submission_id = self._suspended.data.get("submission_id")
        return PendingSuspension(
            submission_id=submission_id if isinstance(submission_id, str) else None,
            requests=self.remaining(),
        )


@dataclass(frozen=True)
class PendingSuspension:
    """最近一次挂起里还没处理的请求。

    Attributes:
        submission_id: 挂起的那一轮的提交标识；没有挂起时为 None。
        requests: 还没处理的请求（内核 ``turn_suspended`` 事件里 ``pending`` 的项）。
    """

    submission_id: str | None
    requests: list[dict[str, Any]]


async def pending_suspension(
    store: ControlPlaneStore, tenant_id: str, session_id: str
) -> PendingSuspension:
    """会话当前等待处理的请求及挂起的那一轮：最近一次挂起里的请求，减去之后已经核销或到期的。"""
    tracker = _PendingTracker()
    after = 0
    while True:
        query = AuditQuery(session_id=session_id, source="engine", limit=_PAGE, after_id=after)
        page = await store.query_audit(tenant_id, query)
        for item in page:
            tracker.observe(item)
        if len(page) < query.limit:
            return tracker.snapshot()
        after = page[-1].audit_id


async def pending_requests(
    store: ControlPlaneStore, tenant_id: str, session_id: str
) -> list[dict[str, Any]]:
    """会话当前等待处理的审批或表单请求：最近一次挂起里的请求，减去之后已经核销或到期的。"""
    return (await pending_suspension(store, tenant_id, session_id)).requests


async def replay_turn(
    store: ControlPlaneStore,
    tenant_id: str,
    session_id: str,
    submission_id: str,
    *,
    after_seq: int = -1,
) -> list[dict[str, Any]]:
    """一轮对话已经记入审计的引擎事件，跳过序号不大于 ``after_seq`` 的。"""
    events: list[dict[str, Any]] = []
    after = 0
    while True:
        page = await store.query_audit(
            tenant_id,
            AuditQuery(
                session_id=session_id,
                submission_id=submission_id,
                source="engine",
                after_id=after,
                limit=_PAGE,
            ),
        )
        for item in page:
            if item.seq is not None and item.seq <= after_seq:
                continue
            data = {key: value for key, value in item.data.items() if key != "submission_id"}
            events.append({"kind": item.kind, "seq": item.seq, "at": item.at, "data": data})
        if len(page) < _PAGE:
            return events
        after = page[-1].audit_id


__all__ = ["PendingSuspension", "pending_requests", "pending_suspension", "replay_turn"]

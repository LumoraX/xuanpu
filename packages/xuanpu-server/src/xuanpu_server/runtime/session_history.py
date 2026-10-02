"""会话的历史视图：只读审计推算出来的东西（还挂着的请求、一轮对话的事件回放）。

审计在控制面里、各实例共享，所以这些读取不要求本实例持有会话。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.errors import InvalidRequestError

if TYPE_CHECKING:
    from collections.abc import Iterable

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

    def remaining(self) -> tuple[dict[str, Any], ...]:
        """还没处理的请求。"""
        if self._suspended is None:
            return ()
        pending = self._suspended.data.get("pending")
        if not isinstance(pending, list):
            return ()
        return tuple(
            dict(item)
            for item in pending
            if isinstance(item, dict) and str(item.get("request_id")) not in self._settled
        )

    def snapshot(self) -> PendingSuspension:
        """还没处理的请求，连同挂起的那一轮的提交标识与已经核销的请求。"""
        if self._suspended is None:
            return PendingSuspension(submission_id=None, requests=())
        # 引擎事件的审计记录都带 submission_id（见 events 模块的 _record）
        submission_id = self._suspended.data.get("submission_id")
        return PendingSuspension(
            submission_id=submission_id if isinstance(submission_id, str) else None,
            requests=self.remaining(),
            settled=frozenset(self._settled),
        )


@dataclass(frozen=True)
class PendingSuspension:
    """最近一次挂起里还没处理的请求。

    Attributes:
        submission_id: 挂起的那一轮的提交标识；没有挂起时为 None。
        requests: 还没处理的请求（内核 ``turn_suspended`` 事件里 ``pending`` 的项）。
        settled: 这次挂起里已经核销的请求。
    """

    submission_id: str | None
    requests: tuple[dict[str, Any], ...]
    settled: frozenset[str] = frozenset()


def refuse_settled(suspension: PendingSuspension, request_ids: Iterable[str]) -> None:
    """送回里有这次挂起已经核销过的请求，就拒绝整次送回。

    内核按这次挂起的全部请求校验送回：部分核销期间，已经核销过的请求再被送回，内核照样受理、
    再做一遍——批准过的工具会再执行一次。所以在提交之前按审计拦下。审计只会滞后（少显示
    已核销），不会多显示，所以不会误拒；滞后的那一小段里的重复送回拦不住。

    Raises:
        InvalidRequestError: 有已经核销过的请求。
    """
    again = sorted(set(request_ids) & suspension.settled)
    if again:
        raise InvalidRequestError(f"待办 {', '.join(again)} 已经处理过，不能再送回")


async def _last_suspension(
    store: ControlPlaneStore, tenant_id: str, session_id: str
) -> AuditRecord | None:
    """会话最近一次挂起（``turn_suspended``）的审计记录；从没挂起过返回 None。"""
    last: AuditRecord | None = None
    after = 0
    while True:
        query = AuditQuery(
            session_id=session_id,
            source="engine",
            kind="turn_suspended",
            limit=_PAGE,
            after_id=after,
        )
        page = await store.query_audit(tenant_id, query)
        if page:
            last = page[-1]
        if len(page) < query.limit:
            return last
        after = page[-1].audit_id


async def pending_suspension(
    store: ControlPlaneStore, tenant_id: str, session_id: str
) -> PendingSuspension:
    """会话当前等待处理的请求及挂起的那一轮：最近一次挂起里的请求，减去之后已经核销或到期的。

    只从最近一次挂起往后读：更早的事件不影响结果（每次挂起都从头算）。审计查询只能按编号
    升序翻页、取不到「最后一条」，所以先按事件种类翻一遍这个会话的挂起记录（库里按会话索引
    过滤，只回挂起记录）找到最后一次，再读它之后的引擎事件。每次送回审批都要读一次。
    """
    tracker = _PendingTracker()
    last = await _last_suspension(store, tenant_id, session_id)
    if last is None:
        return tracker.snapshot()
    after = last.audit_id - 1
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
    return list((await pending_suspension(store, tenant_id, session_id)).requests)


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


__all__ = [
    "PendingSuspension",
    "pending_requests",
    "pending_suspension",
    "refuse_settled",
    "replay_turn",
]

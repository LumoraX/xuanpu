"""过闸记录：``connector_call`` 的字段集合，以及人工拒绝的连接器调用的补记。

闸门（``ConnectorGate``）每次调用连接器工具写一条过闸记录。需要审批的调用被人工拒绝时，
内核直接把「被拒」交给模型、不再调用工具，闸门见不到这次调用；所以由送回审批结果的一方
补一条 ``decision="rejected"``、``detail="rejected_by_user"`` 的记录。两处共用
``gate_call_data``，字段集合只在这里定义。

补记分三步，由 ``SessionManager.resume`` 驱动：

1. ``prepare``：送回之前（内核核销之前）读审计里最近一次挂起还没处理的待办
   （``session_history.pending_suspension``），取出这次被拒的那些，连同详情（工具全名、
   call_id、线程、参数）与挂起的那一轮的提交标识——补的记录归到被拒的那一轮。已经核销过的
   待办不在其中，不再补：它的记录在第一次核销时补过了。
2. ``watch``：按运行时的连接器登记（``ConnectorSet.describe``）挑出连接器工具（连接器、工具、
   传输、副作用分类）。stdio 与 remote 的工具名前缀都是 ``mcp__``，传输只能从登记里查；查不到
   的不是连接器工具（脚本、命令、表单等），不补。提交之后连同这次送回的提交标识登记到事件
   枢纽（``EngineEventHub.watch_resolution``）。
3. 等内核的结论（``resolutions`` 模块）：这次送回核销了待办，只为「被拒、且这一次确实核销了」
   的写记录；被内核整次拒绝（答复不合格、有不认识的待办等）就不写；等不到结论（事件枢纽关闭，
   或这次送回没有结论事件就结束了）记 error 日志。

补记失败不让送回本身失败——审批结果已经交给内核了：记 error 日志并计入 ``failures``，同时经
``on_failure`` 报出是哪个环节（``read`` 读不到待办、``incomplete`` 待办缺调用信息、``write``
写不进记录、``unconfirmed`` 等不到内核的结论；接到平台指标）。

已知限制：

- 参数摘要按审计里待办记下的参数算：那份参数经过脱敏与截断（键名像凭据的字符串、超过
  8000 字符的字符串），碰上这两种情况，与闸门按原始参数算的摘要不同。
- 待办以审计为准（与 ``SessionManager.pending`` 同一口径）：事件枢纽先把挂起事件推给调用方、
  再写审计，紧跟着送回可能落在这中间；挂起事件写库失败、暂留在缓冲里时也一样。这时查不到
  待办，补不上记录。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from xuanpu_server.audit import digest_arguments
from xuanpu_server.runtime.session_history import pending_suspension

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from xuanpu_spec.manifest import Effect, Transport

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.runtime.resolutions import ResolutionWatch
    from xuanpu_server.runtime.session_history import PendingSuspension

logger = logging.getLogger(__name__)

REJECTED_BY_USER = "rejected_by_user"
"""人工拒绝的补记里 ``detail`` 的取值。"""


@dataclass(frozen=True)
class ConnectorToolInfo:
    """一个连接器工具的登记信息。

    Attributes:
        connector: 连接器名。
        transport: 传输方式（``stdio`` / ``remote`` / ``http``）。
        tool: 连接器里的工具名（不带前缀）。
        effect: 副作用分类。
    """

    connector: str
    transport: Transport
    tool: str
    effect: Effect


def gate_call_data(
    info: ConnectorToolInfo,
    *,
    decision: str,
    is_error: bool,
    detail: str,
    call_id: str,
    thread_id: str,
    args: Mapping[str, Any],
    submission_id: object,
    duration_ms: int,
) -> dict[str, Any]:
    """一条 ``connector_call`` 过闸记录的 ``data``。参数只记摘要，不记原文。

    Args:
        info: 被调用的连接器工具。
        decision: ``allowed`` / ``denied`` / ``rejected``。
        is_error: 调用是否出错。
        detail: 简述（出错原因、拒绝缘由）。
        call_id: 内核的工具调用标识。
        thread_id: 发起调用的线程。
        args: 调用参数，只用来算摘要。
        submission_id: 这次调用归属的那一轮的提交标识；内核自己发起的重跑没有，为 None。
        duration_ms: 耗时。
    """
    return {
        "connector": info.connector,
        "transport": info.transport,
        "tool": info.tool,
        "effect": info.effect,
        "decision": decision,
        "is_error": is_error,
        "detail": detail,
        "call_id": call_id,
        "thread_id": thread_id,
        "arguments_digest": digest_arguments(args),
        "submission_id": submission_id,
        "duration_ms": duration_ms,
    }


@dataclass(frozen=True)
class RejectedCall:
    """一次要补记的人工拒绝。

    Attributes:
        tenant_id: 所属租户。
        session_id: 所属会话。
        request_id: 被拒的待办。
        info: 被拒的连接器工具。
        call_id: 内核的工具调用标识。
        thread_id: 发起调用的线程。
        args: 待办里记下的调用参数。
        submission_id: 挂起的那一轮（被拒的那一轮）的提交标识。
    """

    tenant_id: str
    session_id: str
    request_id: str
    info: ConnectorToolInfo
    call_id: str
    thread_id: str
    args: dict[str, Any]
    submission_id: str | None

    def gate_data(self) -> dict[str, Any]:
        """这次拒绝的过闸记录 ``data``：没有调用，不算出错，耗时为 0。"""
        return gate_call_data(
            self.info,
            decision="rejected",
            is_error=False,
            detail=REJECTED_BY_USER,
            call_id=self.call_id,
            thread_id=self.thread_id,
            args=self.args,
            submission_id=self.submission_id,
            duration_ms=0,
        )


def _is_rejection(value: object) -> bool:
    """与内核裁决审批的口径一致：送回的是字典、且 ``granted`` 不为真，就是拒绝。"""
    return isinstance(value, dict) and not bool(value.get("granted"))


def _rejected_call(
    item: Mapping[str, Any],
    describe: Callable[[str], ConnectorToolInfo | None],
    *,
    tenant_id: str,
    session_id: str,
    submission_id: str | None,
) -> RejectedCall | None:
    """一条被拒绝的待办 → 要补记的连接器调用；不是连接器工具的审批返回 None。

    Raises:
        ValueError: 是连接器工具的审批，却缺调用信息（call_id、线程或参数）。
    """
    detail = item.get("detail")
    if item.get("reason") != "permission" or not isinstance(detail, dict):
        return None
    target = detail.get("target")
    if detail.get("scope") != "tool_use" or not isinstance(target, str):
        return None
    info = describe(target)
    if info is None:
        return None
    call_id, thread_id, args = detail.get("call_id"), detail.get("thread_id"), detail.get("args")
    # 连接器工具的待办由闸门发起，必带这三项；缺了说明记录不完整，不能编一条
    if not isinstance(call_id, str) or not isinstance(thread_id, str):
        raise ValueError(f"待办 {item.get('request_id')!r} 缺 call_id 或线程标识")
    if not isinstance(args, dict):
        raise ValueError(f"待办 {item.get('request_id')!r} 缺调用参数")
    return RejectedCall(
        tenant_id=tenant_id,
        session_id=session_id,
        request_id=str(item.get("request_id")),
        info=info,
        call_id=call_id,
        thread_id=thread_id,
        args=args,
        submission_id=submission_id,
    )


@dataclass(frozen=True)
class PendingRejections:
    """送回之前从审计里取出的、这次送回拒绝的待办。

    Attributes:
        tenant_id: 所属租户。
        session_id: 所属会话。
        submission_id: 挂起的那一轮的提交标识。
        requests: 被拒绝的待办（还没区分是不是连接器工具）。
    """

    tenant_id: str
    session_id: str
    submission_id: str | None = None
    requests: tuple[Mapping[str, Any], ...] = ()


class RejectionAudit:
    """人工拒绝的连接器调用的补记：送回之前取出被拒的待办，内核确认核销之后写过闸记录。"""

    def __init__(
        self,
        *,
        store: ControlPlaneStore,
        audit: AuditLog,
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        """
        Args:
            store: 控制面存储，读待办用。
            audit: 审计写入口。
            on_failure: 补记失败时的回调，参数是环节：``read``（读不到待办）、
                ``incomplete``（待办缺调用信息）、``write``（写不进记录）、``unconfirmed``
                （等不到内核的结论）。
        """
        self._store = store
        self._audit = audit
        self._on_failure = on_failure
        self.failures = 0
        """补记失败的累计次数（各环节合计）。"""

    def _failed(self, stage: str) -> None:
        """计一次补记失败，并报给回调。"""
        self.failures += 1
        if self._on_failure is not None:
            self._on_failure(stage)

    async def _pending(self, tenant_id: str, session_id: str) -> PendingSuspension | None:
        """读当前待办；读不到记 error 日志、计一次失败，返回 None。"""
        try:
            return await pending_suspension(self._store, tenant_id, session_id)
        except Exception:
            self._failed("read")
            logger.exception("会话 %s 读不到待办，这次拒绝补不上过闸记录", session_id)
            return None

    async def prepare(
        self, tenant_id: str, session_id: str, resolutions: Mapping[str, Any]
    ) -> PendingRejections:
        """送回之前（内核核销之前）：取出这次送回拒绝的、还没核销的待办。

        送回里没有一项按内核口径算拒绝（表单答案这类不带 ``granted`` 的字典也算，由
        ``watch`` 再按待办的种类排除）时不读审计。

        Args:
            tenant_id: 所属租户。
            session_id: 所属会话。
            resolutions: 这次送回的审批结果（待办标识 → 结果）。
        """
        nothing = PendingRejections(tenant_id=tenant_id, session_id=session_id)
        rejected = [request_id for request_id, value in resolutions.items() if _is_rejection(value)]
        if not rejected:
            return nothing
        suspension = await self._pending(tenant_id, session_id)
        if suspension is None:
            return nothing
        by_id = {str(item.get("request_id")): item for item in suspension.requests}
        return PendingRejections(
            tenant_id=tenant_id,
            session_id=session_id,
            submission_id=suspension.submission_id,
            # 不在还没处理的待办里的（已经核销过的、不存在的）不补
            requests=tuple(by_id[request_id] for request_id in rejected if request_id in by_id),
        )

    def calls(
        self, pending: PendingRejections, describe: Callable[[str], ConnectorToolInfo | None]
    ) -> list[RejectedCall]:
        """被拒的待办里属于连接器工具的那些。缺调用信息的记 error 日志、计一次失败、跳过。

        Args:
            pending: ``prepare`` 取出的被拒待办。
            describe: 按工具全名查连接器登记；查不到（不是连接器工具）返回 None。
        """
        calls: list[RejectedCall] = []
        for item in pending.requests:
            try:
                call = _rejected_call(
                    item,
                    describe,
                    tenant_id=pending.tenant_id,
                    session_id=pending.session_id,
                    submission_id=pending.submission_id,
                )
            except ValueError:
                self._failed("incomplete")
                logger.exception("会话 %s 的拒绝补不上过闸记录", pending.session_id)
                continue
            if call is not None:
                calls.append(call)
        return calls

    def watch(
        self, pending: PendingRejections, describe: Callable[[str], ConnectorToolInfo | None]
    ) -> ResolutionWatch | None:
        """为被拒的连接器调用造一个等内核结论的登记，交给事件枢纽；没有要补的返回 None。"""
        calls = self.calls(pending, describe)
        return _RejectionWatch(self, tuple(calls)) if calls else None

    async def record(self, calls: Sequence[RejectedCall]) -> int:
        """内核确认核销之后：逐条写过闸记录，返回写成了几条。

        某条写失败记 error 日志、计一次失败，不影响其余的，也不往上抛。
        """
        written = 0
        for call in calls:
            try:
                await self._audit.gate(
                    call.tenant_id, "connector_call", call.gate_data(), session_id=call.session_id
                )
            except Exception:
                self._failed("write")
                logger.exception(
                    "会话 %s 的调用 %s 被拒绝，过闸记录没有写进去", call.session_id, call.call_id
                )
                continue
            written += 1
        return written

    def unconfirmed(self, calls: Sequence[RejectedCall]) -> None:
        """等不到内核的结论：这些拒绝补不上记录，记 error 日志，每条计一次失败。"""
        if not calls:
            return
        for _ in calls:
            self._failed("unconfirmed")
        logger.error(
            "会话 %s 等不到内核对这次送回的结论，被拒的调用 %s 没有补过闸记录",
            calls[0].session_id,
            [call.call_id for call in calls],
        )


class _RejectionWatch:
    """一次送回里被拒的连接器调用，等内核的结论再补过闸记录（``ResolutionWatch``）。"""

    def __init__(self, owner: RejectionAudit, calls: tuple[RejectedCall, ...]) -> None:
        """绑定补记方与要补的调用（至少一条）。"""
        self._owner = owner
        self._calls = calls

    async def resolved(self, request_ids: frozenset[str]) -> None:
        """内核核销了这些待办：其中被拒的连接器调用补记录；别的这一次没有核销，不补。"""
        confirmed = [call for call in self._calls if call.request_id in request_ids]
        skipped = [call.call_id for call in self._calls if call.request_id not in request_ids]
        if skipped:
            logger.info(
                "会话 %s 的拒绝 %s 这一次没有被核销（之前已经核销过），不再补过闸记录",
                self._calls[0].session_id,
                skipped,
            )
        await self._owner.record(confirmed)

    def refused(self, reason: str) -> None:
        """内核整次拒绝了这次送回：什么都没核销，不补。"""
        logger.info(
            "会话 %s 的送回被内核拒绝（%s），被拒的调用 %s 不补过闸记录",
            self._calls[0].session_id,
            reason,
            [call.call_id for call in self._calls],
        )

    def abandoned(self) -> None:
        """等不到结论了。"""
        self._owner.unconfirmed(self._calls)


__all__ = [
    "REJECTED_BY_USER",
    "ConnectorToolInfo",
    "PendingRejections",
    "RejectedCall",
    "RejectionAudit",
    "gate_call_data",
]

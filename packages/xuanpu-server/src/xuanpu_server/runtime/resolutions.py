"""送回审批的结论：把内核的核销事件对到在等结论的那次送回上。

送回审批（``Resume``）提交之后，内核要么核销其中的待办（``suspension_resolved``：整个挂起的
待办都处理完了；``suspension_partially_resolved``：只处理了一部分），要么整次拒绝
（``suspension_resolve_rejected``：没有活跃的挂起、答复不合格、有不认识的待办等，什么都没
核销）。这三种事件都带着那次送回的提交标识。要据此做事的一方（例如补人工拒绝的过闸记录）
提交之后在这里登记，等结论到了再做；只凭「提交成功」就做，会把内核随后整次拒绝的、或已经
核销过的待办也算进去。

「这一次新核销了哪些」要算：``suspension_partially_resolved`` 的 ``resolved_request_ids`` 是这一次
核销的；``suspension_resolved`` 的 ``request_ids`` 却是整个挂起的全部待办，要减去之前几次部分
核销过的。部分核销期间，已经核销的待办再被送回，内核照样受理、照样列进去，同样靠这个差集
不算第二次。差集只覆盖本引擎（本事件枢纽）见过的核销；之前的部分核销发生在别的实例或引擎
上一次的生命周期里时，由登记方按审计里的待办自己排除已核销的。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    import taifeng

logger = logging.getLogger(__name__)

RESOLUTION_KINDS: frozenset[str] = frozenset(
    {"suspension_resolved", "suspension_partially_resolved", "suspension_resolve_rejected"}
)
"""送回审批的结论事件。"""


class ResolutionWatch(Protocol):
    """一次送回审批在等内核的结论。"""

    async def resolved(self, request_ids: frozenset[str]) -> None:
        """内核核销了这次送回里的这些待办（只含这一次新核销的）。"""
        ...

    def refused(self, reason: str) -> None:
        """内核整次拒绝了这次送回，什么都没核销。"""
        ...

    def abandoned(self) -> None:
        """等不到结论了：事件枢纽关闭，或这次送回没有结论事件就结束了。"""
        ...


class ResolutionLedger:
    """一个引擎的送回审批登记：哪几次送回在等结论，每个挂起已经核销了哪些待办。"""

    def __init__(self, session_id: str) -> None:
        """``session_id`` 只用来写日志。"""
        self._session_id = session_id
        self._watches: dict[str, ResolutionWatch] = {}
        # 挂起记录 → 本引擎见过的、部分核销时已经核销的待办；整个挂起处理完就移除
        self._settled: dict[str, set[str]] = {}

    def watch(self, submission_id: str, watch: ResolutionWatch) -> None:
        """登记一次送回在等结论。要在提交返回之后、任何让出点之前调用，否则结论可能先到。"""
        self._watches[submission_id] = watch

    async def settle(self, event: taifeng.EventMsg) -> None:
        """处理一条结论事件：记下核销了哪些待办，交给在等这次送回的一方。

        等待方处理时出的错只记日志，不往上抛：这里跑在事件枢纽的消费循环里，抛出去会让整个
        引擎的事件分发停下。
        """
        kind, data = event.msg.kind, event.msg.data
        watch = self._watches.pop(event.submission_id, None)
        if kind == "suspension_resolve_rejected":
            if watch is not None:
                watch.refused(str(data.get("reason")))
            return
        newly = self._newly_settled(kind, data)
        if watch is None:
            return
        if newly is None:
            logger.error(
                "会话 %s 的核销事件 %s 缺待办标识或挂起记录标识，等这次送回的一方得不到结论",
                self._session_id,
                kind,
            )
            watch.abandoned()
            return
        try:
            await watch.resolved(newly)
        except Exception:
            logger.exception(
                "会话 %s 的送回 %s 已被内核核销，后续处理出错",
                self._session_id,
                event.submission_id,
            )

    def _newly_settled(self, kind: str, data: Mapping[str, object]) -> frozenset[str] | None:
        """这条核销事件新核销的待办；事件缺字段返回 None。"""
        field = "request_ids" if kind == "suspension_resolved" else "resolved_request_ids"
        ids, record_id = data.get(field), data.get("record_id")
        if not isinstance(ids, list) or not isinstance(record_id, str):
            return None
        if kind == "suspension_resolved":
            # 整个挂起处理完了：之后不会再有它的核销事件
            seen = self._settled.pop(record_id, set())
        else:
            seen = self._settled.setdefault(record_id, set())
        newly = frozenset(str(item) for item in ids) - seen
        seen.update(newly)
        return newly

    def ended(self, submission_id: str) -> None:
        """一次提交结束了。它若还在等结论（没有结论事件就结束了），就告诉等待方等不到了。"""
        watch = self._watches.pop(submission_id, None)
        if watch is not None:
            watch.abandoned()

    def close(self) -> None:
        """事件枢纽关闭：还在等的都等不到了。"""
        watches, self._watches = list(self._watches.values()), {}
        self._settled.clear()
        for watch in watches:
            watch.abandoned()


__all__ = ["RESOLUTION_KINDS", "ResolutionLedger", "ResolutionWatch"]

"""管家调整自己的时限与预算（R6，ADR 0020）：随任务伸缩，但有上限、要说明理由、全部留痕。

- ``extend_turn``：把本轮的截止时间延后，至多到本轮开始后 ``max_turn_seconds``。
- ``raise_budget``：提高这次请求的 token、管家轮数或匠人数预算，至多到 ``*_ceiling``；
  租户每日 token 配额不受影响。提高值记在审计里（``butler_budget_raised``），
  ``budget_config`` 读出后叠加到起始预算上，唤醒的后续轮次与其他实例都看得到。
- 每次调整写一条控制面审计；结果作为工具输出交给模型，由它在回复里告诉员工。
- 主动延长的提示（``time_hint``）：本轮用掉六成时限后，每个工具输出末尾附上剩余秒数，
  提醒管家在被截断前延长或把剩下的交出去；只是提示，不替管家延长。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import taifeng

from server.core.controlplane.models import AuditQuery
from server.engine.butler.config import BUDGET_KINDS

if TYPE_CHECKING:
    from server.engine.butler.config import ButlerConfig
    from server.engine.butler.turn import ButlerTurn
    from server.engine.tracing.models import TraceOwner
    from server.platform import Platform

_REASON_MAX = 500
_HINT_AT = 0.6
_RAISED = "butler_budget_raised"
_RAISES_SCANNED = 1000


async def budget_config(platform: Platform, owner: TraceOwner, trace_id: str) -> ButlerConfig:
    """这条 trace 生效的预算：起始预算叠加管家提高过的值（只升不降，不超过上限）。"""
    config = platform.butler_config
    records = await platform.store.query_audit(owner.tenant_id, AuditQuery(
        kind=_RAISED, user_id=owner.user_id, limit=_RAISES_SCANNED))
    raised: dict[str, int] = {}
    for record in records:
        data = record.data or {}
        kind, limit = data.get("budget"), data.get("limit")
        if data.get("trace_id") != trace_id or kind not in BUDGET_KINDS or not isinstance(limit, int):
            continue
        raised[kind] = max(raised.get(kind, 0), limit)
    update = {}
    for kind, limit in raised.items():
        start = getattr(config, f"max_{kind}_per_trace")
        ceiling = getattr(config, f"max_{kind}_per_trace_ceiling")
        update[f"max_{kind}_per_trace"] = max(start, min(limit, ceiling))
    return config.model_copy(update=update) if update else config


def _reason(args: dict[str, Any]) -> str | None:
    """理由必填，去空白后截断；空理由返回 None。"""
    text = str(args.get("reason") or "").strip()
    return text[:_REASON_MAX] or None


def time_hint(turn: ButlerTurn) -> str | None:
    """本轮已用掉六成以上时限时给出剩余时间提示；没有时限或还早时返回 None。"""
    deadline = turn.deadline
    when = deadline.when() if deadline is not None else None
    if when is None:
        return None
    now = asyncio.get_running_loop().time()
    window = when - turn.started_at
    if window <= 0 or now - turn.started_at < window * _HINT_AT:
        return None
    remaining = max(0, round(when - now))
    return (f"\n（本轮剩余约 {remaining} 秒。这件事还需要更久的话，先调用 extend_turn 说明理由延长，"
            "或把剩下的交给匠人；不要做到一半被截断。）")


class LimitTools:
    """时限与预算工具的处理函数；身份一律取本轮上下文。"""

    def __init__(self, platform: Platform) -> None:
        """平台注入。"""
        self._platform = platform

    async def extend_turn(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                          ) -> taifeng.ToolResult:
        """从现在起再给本轮 ``seconds`` 秒；超过上限按上限截断，并如实告知。"""
        reason = _reason(args)
        seconds = args.get("seconds")
        if reason is None or not isinstance(seconds, int) or seconds <= 0:
            return taifeng.ToolResult.error("invalid_arguments: seconds 须为正整数，reason 必填")
        if turn.deadline is None:
            return taifeng.ToolResult.error("本轮没有可延长的时限")
        config = self._platform.butler_config
        now = asyncio.get_running_loop().time()
        ceiling = turn.started_at + config.max_turn_seconds
        current = turn.deadline.when() or now
        target = min(now + seconds, ceiling)
        if target <= current:
            return taifeng.ToolResult.ok(json.dumps({
                "extended": False, "remaining_seconds": round(current - now),
                "note": "当前时限已不早于所请求的时间" if current < ceiling else "已到本轮时限上限"},
                ensure_ascii=False))
        turn.deadline.reschedule(target)
        capped = target >= ceiling
        await self._platform.audit.control(
            turn.who, turn.who.require_tenant(), "butler_turn_extended",
            {"trace_id": turn.trace_id, "request_id": turn.request_id, "reason": reason,
             "requested_seconds": seconds, "remaining_seconds": round(target - now),
             "capped": capped})
        return taifeng.ToolResult.ok(json.dumps({
            "extended": True, "remaining_seconds": round(target - now), "capped": capped,
            "note": "已到本轮时限上限，更长的工作请拆成派工或编组" if capped else ""},
            ensure_ascii=False))

    async def raise_budget(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                           ) -> taifeng.ToolResult:
        """把这次请求的某项预算提高到 ``to``；只能提高，超过上限按上限截断并如实告知。"""
        reason, kind, target = _reason(args), args.get("budget"), args.get("to")
        if reason is None or kind not in BUDGET_KINDS or not isinstance(target, int):
            return taifeng.ToolResult.error(
                "invalid_arguments: budget 须为 tokens、butler_turns 或 artisans，to 为整数，reason 必填")
        current = getattr(await budget_config(self._platform, turn.owner(), turn.trace_id),
                          f"max_{kind}_per_trace")
        ceiling = getattr(self._platform.butler_config, f"max_{kind}_per_trace_ceiling")
        limit = min(target, ceiling)
        if limit <= current:
            return taifeng.ToolResult.ok(json.dumps({
                "raised": False, "limit": current, "ceiling": ceiling,
                "note": "当前预算已不低于所请求的值" if current < ceiling else "已到这项预算的上限"},
                ensure_ascii=False))
        await self._platform.audit.control(
            turn.who, turn.who.require_tenant(), _RAISED,
            {"trace_id": turn.trace_id, "request_id": turn.request_id, "budget": kind,
             "from": current, "limit": limit, "requested": target, "reason": reason})
        return taifeng.ToolResult.ok(json.dumps({
            "raised": True, "from": current, "limit": limit, "capped": limit < target,
            "note": "请在回复里告诉员工这次提高了多少、为什么"}, ensure_ascii=False))

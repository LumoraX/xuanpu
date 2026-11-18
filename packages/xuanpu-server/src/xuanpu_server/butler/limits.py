"""管家调整自己的时限与预算（R6，ADR 0020）：随任务伸缩，但有上限、要说明理由、全部留痕。

- ``extend_turn``：把本轮的截止时间延后，至多到本轮开始后 ``max_turn_seconds``。
- 每次调整写一条控制面审计；结果作为工具输出交给模型，由它在回复里告诉员工。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import taifeng

if TYPE_CHECKING:
    from xuanpu_server.butler.turn import ButlerTurn
    from xuanpu_server.platform import Platform

_REASON_MAX = 500


def _reason(args: dict[str, Any]) -> str | None:
    """理由必填，去空白后截断；空理由返回 None。"""
    text = str(args.get("reason") or "").strip()
    return text[:_REASON_MAX] or None


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

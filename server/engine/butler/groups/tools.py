"""编组的管家工具处理函数：提交计划、取消、查进展。身份一律取自本轮上下文。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import taifeng

if TYPE_CHECKING:
    from server.engine.butler.groups.plan import GroupRecord
    from server.engine.butler.groups.runner import GroupRunner
    from server.engine.butler.turn import ButlerTurn

_MEMBER = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "package": {"type": "string"}, "entry": {"type": "string"},
        "brief": {"type": "string", "description": "给这位匠人的任务说明"},
        "materials": {"type": "array", "items": {
            "type": "object", "properties": {"item_id": {"type": "string"},
                                             "revision": {"type": "integer"}},
            "required": ["item_id", "revision"]}},
    },
    "required": ["package", "entry", "brief"],
}
GROUP_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "steps": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "mode": {"type": "string", "enum": ["parallel", "relay", "consult"]},
                "members": {"type": "array", "items": _MEMBER},
                # 允许 null：部分网关按严格模式要求模型填满每个字段，只给两个枚举值时
                # 第一步只能填一个必然被拒的值（真实模型验收中连续被拒直至一轮超时）。
                "when": {"type": ["string", "null"], "enum": ["completed", "failed", None],
                         "description": "第一步写 null；之后的步可选，只在前一步是该结果时进行"},
            },
            "required": ["mode", "members"]}},
    },
    "required": ["title", "steps"],
}


def _json(data: object) -> str:
    """工具输出统一成 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False, default=str)


def group_view(record: GroupRecord) -> dict[str, Any]:
    """给管家看的编组进展：步、成员结果，不含匠人正文。"""
    return {
        "group_id": record.group_id, "state": record.state, "step": record.step,
        "cancel_requested": record.cancel_requested, "summarized": record.summarized,
        "members": [{"step": r.step, "index": r.index, "dispatch_id": r.dispatch_id,
                     "outcome": r.outcome, "end_reason": r.end_reason} for r in record.runs],
    }


class GroupTools:
    """编组工具。"""

    def __init__(self, runner: GroupRunner) -> None:
        """依赖由平台注入。"""
        self._runner = runner

    async def dispatch_group(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                             ) -> taifeng.ToolResult:
        """校验并提交计划；第一步立即启动，不等结果。"""
        record = await self._runner.submit(turn, args.get("plan"))
        return taifeng.ToolResult.ok(_json(group_view(record)))

    async def cancel_group(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                           ) -> taifeng.ToolResult:
        """请求取消；以成员实际停下为准。"""
        record = await self._runner.cancel(turn.owner(), str(args["group_id"]))
        return taifeng.ToolResult.ok(_json({**group_view(record),
                                            "note": "已请求停止，结果以匠人实际停下为准"}))

    async def check_group(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                          ) -> taifeng.ToolResult:
        """本人编组的进展；他人的与不存在的一样不可见。"""
        record = await self._runner.get(turn.owner(), str(args["group_id"]))
        return taifeng.ToolResult.ok(_json(group_view(record)))

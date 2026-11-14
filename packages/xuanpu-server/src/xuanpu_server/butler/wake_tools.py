"""R5-C2 新增的管家工具处理函数：取消派工、答复待处理项、代答、转问。

代答与转问只在被唤醒的一轮里登记意图，真正的转发与提问由唤醒处理按持久决定执行，
进程中断后不会重复。答复待处理项转发的是员工本句原文，不用模型改写的文本。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import taifeng

from xuanpu_server.butler.proxy import check_proxy
from xuanpu_server.butler.wake_models import AnswerSource
from xuanpu_server.errors import ConflictError, InvalidRequestError

if TYPE_CHECKING:
    from xuanpu_server.butler.pending import PendingService
    from xuanpu_server.butler.turn import ButlerTurn
    from xuanpu_server.platform import Platform

QUESTION_MAX = 2000


def _json(data: object) -> str:
    """工具输出统一成 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False)


class WakeTools:
    """处理函数集合。"""

    def __init__(self, platform: Platform, pending: PendingService) -> None:
        """依赖由平台注入。"""
        self._platform = platform
        self._pending = pending

    async def cancel(self, turn: ButlerTurn, args: dict[str, Any], _call: str) -> taifeng.ToolResult:
        """沿用 R2 口径：登记停止意图并通知宿主，终态以宿主实际结果为准。"""
        record = await self._platform.dispatches.get(turn.owner(), str(args["dispatch_id"]))
        if not record.active or record.host_ref is None:
            return taifeng.ToolResult.error("这次派工已经结束，无需取消")
        if record.host == "task":
            await self._platform.work_tasks.cancel(turn.who, record.host_ref)
        else:
            targets = await self._platform.sessions.cancel(turn.who, record.host_ref)
            if not targets and record.state == "asking":
                return taifeng.ToolResult.error("匠人正等你确认一项操作，请在待确认里拒绝它")
        return taifeng.ToolResult.ok(_json({"dispatch_id": record.dispatch_id,
                                            "cancel_requested": True,
                                            "note": "已请求停止，结果以匠人实际停下为准"}))

    async def answer_pending(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                             ) -> taifeng.ToolResult:
        """把员工本句原文转给对应匠人。"""
        if not turn.user_text:
            raise InvalidRequestError("没有可转交的员工原话")
        item = await self._pending.answer(turn.who, str(args["pending_id"]), turn.user_text,
                                          request_id=f"{turn.request_id}:pa")
        turn.answered.append(item.pending_id)
        return taifeng.ToolResult.ok(_json({"pending_id": item.pending_id, "state": item.state}))

    async def answer_artisan(self, turn: ButlerTurn, args: dict[str, Any], _call: str,
                             ) -> taifeng.ToolResult:
        """登记代答；先按出处规则初核，不合规直接告诉管家改为转问。"""
        wake = turn.wake
        assert wake is not None
        if not wake.can_answer:
            raise ConflictError("匠人这一轮没有正常结束，不能代答")
        source = AnswerSource(kind=str(args["source_kind"]), quote=str(args["quote"]))  # type: ignore[arg-type]
        answer = str(args["answer"]).strip()
        verdict = check_proxy(wake.artisan_text, answer, source, wake.corpus)
        if not verdict.allowed:
            wake.proxy_rejected = True
            return taifeng.ToolResult.error(
                f"不能代答（{verdict.reason}）：只能逐字使用员工说过的事实，"
                "涉及授权、金额、对外发送、审批的必须转问员工。请改用 ask_user。")
        wake.answer = (answer, source)
        return taifeng.ToolResult.ok(_json({"registered": True}))

    async def ask_user(self, turn: ButlerTurn, args: dict[str, Any], _call: str) -> taifeng.ToolResult:
        """登记转问员工的问题。"""
        wake = turn.wake
        assert wake is not None
        question = str(args["question"]).strip()
        if not question or len(question) > QUESTION_MAX:
            raise InvalidRequestError(f"问题须为 1–{QUESTION_MAX} 个字符")
        wake.question = question
        return taifeng.ToolResult.ok(_json({"registered": True}))

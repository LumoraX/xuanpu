"""停止生成：取消会话里在途的对话。

内核的 ``CancelTurn`` 按目标那一轮所处的状态处理：

- 正在跑：那一轮以 ``turn_completed`` 收尾，``end_reason="cancelled"``、``success=False``、
  ``cancel_reason="requested"``（不是 ``turn_failed``）；
- 还在排队：以 ``turn_failed`` 收尾，``kind="cancelled"``；
- 已挂起：清掉挂起记录——平台不走这条路，挂起那一轮已不在途，拒绝审批仍走 ``resume``；
- 找不到：静默无操作。所以「在不在途」由平台按自己登记的在途集合先判定，不交给内核。

两种收尾事件都带 ``is_root``，事件枢纽照常据此结束那一轮的事件流、移出在途集合。
CancelTurn 自己那个 submission 上内核另发一条 ``engine_log``：平台不为它登记事件流、
不算在途，它只进审计。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import taifeng

from xuanpu_server.errors import InvalidRequestError

if TYPE_CHECKING:
    from collections.abc import Collection


def pick_targets(in_flight: Collection[str], submission_id: str | None) -> list[str]:
    """要停的 submission：指定了就只停它（不在途是请求不合法），否则停全部在途的。"""
    if submission_id is None:
        return sorted(in_flight)
    if submission_id not in in_flight:
        raise InvalidRequestError(f"submission {submission_id!r} 不在途（已经结束或不存在）")
    return [submission_id]


async def cancel_in_flight(
    engine: taifeng.AgentEngine, in_flight: Collection[str], submission_id: str | None
) -> list[str]:
    """向内核逐个提交 ``CancelTurn``，返回停了哪些 submission。

    只是提交取消请求：返回时目标那一轮可能还没收尾，收尾事件照常从它自己的事件流出来。

    Args:
        engine: 会话的引擎。
        in_flight: 本实例登记的在途 submission。先取快照，提交期间它可能变化。
        submission_id: 只停这一轮；省略则停全部在途的。
    """
    targets = pick_targets(in_flight, submission_id)
    for target in targets:
        await engine.submit(taifeng.CancelTurn(submission_id=target))
    return targets


__all__ = ["cancel_in_flight", "pick_targets"]

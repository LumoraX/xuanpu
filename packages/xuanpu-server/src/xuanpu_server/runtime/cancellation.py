"""停止生成：取消会话里在途的对话。

内核的 ``CancelTurn`` 只看它自己的在飞表（``introspect()["pending_submissions"]``），按目标
那一轮所处的状态处理：

- 正在跑：那一轮以 ``turn_completed`` 收尾，``end_reason="cancelled"``、``success=False``、
  ``cancel_reason="requested"``（不是 ``turn_failed``）；
- 还在排队：以 ``turn_failed`` 收尾，``kind="cancelled"``；
- 不在在飞表里、但有它的活跃挂起：清掉挂起记录（另发一条 ``engine_log``，不发收尾事件）；
- 都不是：静默无操作。

所以平台不能把「本实例登记为在途」直接当成「内核可以取消」：

- 刚提交的一轮要等内核为它起了任务才进在飞表，紧跟着提交的取消会被静默丢掉；
- 内核已发出 ``turn_suspended``、事件枢纽还没处理时，平台仍把它算在途，内核却会把取消
  当成「清掉挂起」——平台的待办视图还在，之后批准它的事件流永不收尾。

因此逐个目标先等它出现在内核的在飞表里再提交；等待期间它离开了平台的在途集合（已经
收尾或挂起）就跳过；等到上限仍未受理也跳过并记警告。只返回真正提交了取消的目标。

剩下的窗口要靠内核修：从确认受理到内核 actor 处理这条 ``CancelTurn``，中间还有约一个
事件循环迭代；目标恰在这个窗口里挂起，挂起记录仍会被清掉（或恰好收尾，取消落空）。

两种收尾事件都带 ``is_root``，事件枢纽照常据此结束那一轮的事件流、移出在途集合。
CancelTurn 自己那个 submission 上内核另发一条 ``engine_log``：平台不为它登记事件流、
不算在途，它只进审计。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import taifeng

from xuanpu_server.errors import InvalidRequestError

if TYPE_CHECKING:
    from collections.abc import Collection

logger = logging.getLogger(__name__)

# 等内核受理一轮最多等多久。刚提交的一轮通常几个事件循环迭代、几毫秒就进在飞表；
# 等不到多半是它已经挂起或收尾、事件枢纽还没处理完，久等无益
ADMISSION_WAIT_SECONDS = 1.0
# 轮询间隔从 1 毫秒起逐次翻倍、封顶 50 毫秒：introspect 每次都算一遍上下文估算，不宜空转
_POLL_FIRST_SECONDS = 0.001
_POLL_MAX_SECONDS = 0.05


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
    """等内核受理了目标再逐个提交 ``CancelTurn``，返回真正提交了取消的 submission（按提交先后）。

    只是提交取消请求：返回时目标那一轮可能还没收尾，收尾事件照常从它自己的事件流出来。

    Args:
        engine: 会话的引擎。
        in_flight: 本实例登记的在途集合（活的：等待期间据它判断目标是否已经收尾或挂起）。
        submission_id: 只停这一轮；省略则停全部在途的。
    """
    waiting = pick_targets(in_flight, submission_id)
    submitted: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ADMISSION_WAIT_SECONDS
    pause = _POLL_FIRST_SECONDS
    while waiting:
        admitted = set(engine.introspect()["pending_submissions"])
        for target in list(waiting):
            if target in admitted:
                await engine.submit(taifeng.CancelTurn(submission_id=target))
                submitted.append(target)
                waiting.remove(target)
            elif target not in in_flight:
                # 已经收尾或挂起：不在这里停
                waiting.remove(target)
        if not waiting:
            break
        if loop.time() >= deadline:
            logger.warning(
                "等了 %.1f 秒内核仍未受理 %s，不提交取消", ADMISSION_WAIT_SECONDS, waiting
            )
            break
        await asyncio.sleep(pause)
        pause = min(pause * 2, _POLL_MAX_SECONDS)
    return submitted


__all__ = ["ADMISSION_WAIT_SECONDS", "cancel_in_flight", "pick_targets"]

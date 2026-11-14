"""会诊比对的模型评级（R5-D1）：用管家引擎跑一轮「比对」，管家经 ``rate_divergence`` 登记评级。

- 输入全是数据：问题（计划标题与任务说明）与各成员发布的结论；不含成员的内部过程。
- 这一轮只能调用 ``rate_divergence``；没登记评级、预算用尽或模型不可用都返回 None，
  由调用方按「评不出」交员工裁决。评级只决定走哪条路。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from xuanpu_server.butler.budget import ButlerBudgetError, check_butler_turn
from xuanpu_server.butler.review import trace_usage
from xuanpu_server.butler.turn import ButlerTurn, CompareContext
from xuanpu_server.errors import PlatformError
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from xuanpu_server.butler.engine import ButlerEngines
    from xuanpu_server.butler.groups.divergence import MemberConclusion, Rating
    from xuanpu_server.platform import Platform
    from xuanpu_server.tracing.models import TraceOwner

logger = logging.getLogger(__name__)
COMPARE_PURPOSE = "consult_compare"
_CONCLUSION_MAX = 3000


class ButlerDivergenceRater:
    """管家引擎上的比对评级。"""

    def __init__(self, platform: Platform, engines: ButlerEngines) -> None:
        """依赖由平台注入。"""
        self._platform = platform
        self._engines = engines

    async def rate(self, owner: TraceOwner, trace_id: str, question: str,
                   conclusions: tuple[MemberConclusion, ...]) -> Rating | None:
        """跑一轮比对；评不出返回 None。"""
        try:
            check_butler_turn(self._platform.butler_config,
                              await trace_usage(self._platform, owner, trace_id))
        except ButlerBudgetError as exc:
            logger.info("会诊比对跳过（%s）", exc.reason)
            return None
        who = Principal(role="tenant_member", tenant_id=owner.tenant_id, user_id=owner.user_id,
                        key_id=None)
        settings = await self._platform.workspace.settings(who)
        compare = CompareContext(members=len(conclusions))
        turn = ButlerTurn(who=who, request_id=f"cmp-{trace_id}", trace_id=trace_id,
                          timezone=str(settings["timezone"]), compare=compare)
        try:
            await self._engines.run(turn, payload(question, conclusions))
        except PlatformError as exc:
            logger.info("会诊比对不可用（%s）", exc.code)
            return None
        return compare.rating


def payload(question: str, conclusions: tuple[MemberConclusion, ...]) -> str:
    """比对一轮的输入：全部是数据。"""
    return json.dumps({
        "purpose": COMPARE_PURPOSE, "question": question[:4000],
        "conclusions": [{"member": c.number, "artisan": c.artisan,
                         "conclusion": c.text[:_CONCLUSION_MAX]} for c in conclusions],
    }, ensure_ascii=False)

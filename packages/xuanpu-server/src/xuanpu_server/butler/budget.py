"""请求预算：每条 trace 的匠人数、管家轮数、token，与租户并发 trace。全部是纯函数。

超限抛 ``ButlerBudgetError``（HTTP 429，带稳定原因码），调用方据此明确阻塞并保留员工输入，
不建任何派工记录。用量未知时按「无法证明未超」处理：token 上限检查要求已知用量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from xuanpu_server.errors import QuotaExceededError

if TYPE_CHECKING:
    from xuanpu_server.butler.config import ButlerConfig

BudgetReason = Literal[
    "artisans_per_trace", "butler_turns_per_trace", "tokens_per_trace",
    "active_traces_per_tenant", "tokens_unknown",
]


class ButlerBudgetError(QuotaExceededError):
    """管家预算超限。``reason`` 是稳定原因码，文案只说明是哪一项。"""

    code = "butler_budget_exceeded"

    def __init__(self, reason: BudgetReason, message: str) -> None:
        """记录原因码。"""
        super().__init__(message)
        self.reason: BudgetReason = reason


@dataclass(frozen=True)
class TraceUsage:
    """一条 trace 已用的预算。

    Attributes:
        artisans: 已建的派工数（含已结束的）。
        butler_turns: 管家已跑的轮数。
        tokens: 已知的 token 合计；None 表示有尝试的用量未知。
    """

    artisans: int = 0
    butler_turns: int = 0
    tokens: int | None = 0


def check_tokens(config: ButlerConfig, usage: TraceUsage) -> None:
    """token 已知且未达上限；未知时不放行（不能把未知当 0）。"""
    if usage.tokens is None:
        raise ButlerBudgetError("tokens_unknown", "本次请求的用量暂时无法核实，已暂停继续派工")
    if usage.tokens >= config.max_tokens_per_trace:
        raise ButlerBudgetError("tokens_per_trace", "本次请求的 token 用量已达上限")


def check_butler_turn(config: ButlerConfig, usage: TraceUsage) -> None:
    """管家再跑一轮之前检查：轮数与 token。"""
    if usage.butler_turns >= config.max_butler_turns_per_trace:
        raise ButlerBudgetError("butler_turns_per_trace", "本次请求的管家处理轮数已达上限")
    check_tokens(config, usage)


def check_dispatch(
    config: ButlerConfig, usage: TraceUsage, *, active_traces: int, trace_active: bool,
) -> None:
    """派出一个新匠人之前检查。

    Args:
        config: 管家配置。
        usage: 这条 trace 已用的预算。
        active_traces: 租户当前有进行中派工的 trace 数。
        trace_active: 这条 trace 自己是否已在其中（已在则不额外占并发名额）。
    """
    if usage.artisans >= config.max_artisans_per_trace:
        raise ButlerBudgetError("artisans_per_trace", "本次请求可派出的匠人数已达上限")
    check_tokens(config, usage)
    if not trace_active and active_traces >= config.max_active_traces_per_tenant:
        raise ButlerBudgetError("active_traces_per_tenant", "企业当前进行中的派工过多，请稍后再试")

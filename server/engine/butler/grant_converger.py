"""定时/主动自动化「试运行 → Trace 自动收敛最小权限 Grant」纯函数引擎（ADR 0024 / 0025）。

- 从一次真实试运行的执行轨迹（``TraceView``）与派工记录中提取实际调用到的
  精确匠人版本、工具清单与实测 Token 消耗；
- 按 ``实测 Token × 1.5`` 给出单次预算建议（封顶受平台硬上限约束）；
- 严格禁止通配符（``*``）进入任何授权项，保证定时无人值守任务遵循最小权限原则。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.core.errors import InvalidRequestError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from server.engine.butler.models import DispatchRecord
    from server.engine.tracing.models import TraceView

MIN_BUDGET_TOKENS = 10_000
MAX_BUDGET_TOKENS = 2_000_000
BUDGET_SAFETY_MULTIPLIER = 1.5


class ConvergedGrantProposal(BaseModel):
    """由试运行 Trace 自动收敛出的最小权限 Grant 提案（供员工一键确认启用）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_trace_id: str = Field(min_length=1, max_length=128)
    artisan_refs: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    observed_tokens: int = Field(ge=0)
    recommended_max_tokens: int = Field(ge=MIN_BUDGET_TOKENS, le=MAX_BUDGET_TOKENS)
    require_approval_on_unlisted: bool = True

    @field_validator("artisan_refs", "allowed_tools")
    @classmethod
    def _forbid_wildcards(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """反通配符铁律：严禁出现 ``*`` 级宽泛授权。"""
        for item in values:
            if not item or "*" in item:
                raise ValueError(f"最小权限 Grant 禁止通配符或空项：{item!r}")
        return values


def converge_grant_from_trace(
    trace: TraceView,
    dispatches: Sequence[DispatchRecord] = (),
    *,
    budget_ceiling: int = MAX_BUDGET_TOKENS,
) -> ConvergedGrantProposal:
    """从试运行 Trace 与派工记录确定性收敛最小权限 Grant 提案。"""
    if not trace.spans:
        raise InvalidRequestError("试运行 Trace 没有任何执行记录，无法收敛权限")

    tools: set[str] = set()
    observed_tokens = 0
    for span in trace.spans:
        if span.kind == "tool" and span.name:
            name = span.name.strip()
            if name and "*" not in name:
                tools.add(name)
        for attempt in span.attempts:
            if attempt.input_tokens:
                observed_tokens += attempt.input_tokens
            if attempt.output_tokens:
                observed_tokens += attempt.output_tokens

    artisans: set[str] = set()
    for d in dispatches:
        ref = f"{d.artisan.package}@{d.artisan.version}:{d.artisan.entry}"
        if "*" not in ref:
            artisans.add(ref)

    raw_budget = max(MIN_BUDGET_TOKENS, math.ceil(observed_tokens * BUDGET_SAFETY_MULTIPLIER))
    bounded_budget = min(raw_budget, max(MIN_BUDGET_TOKENS, min(budget_ceiling, MAX_BUDGET_TOKENS)))
    return ConvergedGrantProposal(
        source_trace_id=trace.trace.trace_id,
        artisan_refs=tuple(sorted(artisans)),
        allowed_tools=tuple(sorted(tools)),
        observed_tokens=observed_tokens,
        recommended_max_tokens=bounded_budget,
        require_approval_on_unlisted=True,
    )

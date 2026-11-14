"""编组计划的服务端校验：结构上限、成员匠人须在本人当前清单内、材料归属、预算。

校验不通过一律报错且不建任何记录；模型给的计划只是数据。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from xuanpu_server.butler.budget import ButlerBudgetError, TraceUsage, check_tokens
from xuanpu_server.butler.groups.plan import GroupPlan
from xuanpu_server.errors import InvalidRequestError

if TYPE_CHECKING:
    from xuanpu_server.butler.config import ButlerConfig
    from xuanpu_server.butler.resolver import ArtisanResolver
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.models import InputFile


def parse_plan(raw: Any, config: ButlerConfig) -> GroupPlan:
    """严格解析并核对上限；结构错误报请求错误，只给错误位置不回显内容。"""
    try:
        plan = GroupPlan.model_validate(raw)
    except ValidationError as exc:
        where = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        raise InvalidRequestError(f"编组计划格式不对：{', '.join(where)[:300]}") from exc
    if len(plan.steps) > config.max_group_steps:
        raise InvalidRequestError(f"编组计划最多 {config.max_group_steps} 步")
    for step in plan.steps:
        if len(step.members) > config.max_group_members:
            raise InvalidRequestError(f"编组每一步最多 {config.max_group_members} 个成员")
    return plan


async def check_members(
    plan: GroupPlan, who: Principal, artisans: ArtisanResolver,
    provided: tuple[InputFile, ...],
) -> None:
    """每个成员的匠人都须对本人此刻可用；材料只能是本人本次提供的，会话承载不收材料。

    Raises:
        NotFoundError: 某个匠人对本人不可用（不区分原因）。
        InvalidRequestError: 材料不合规。
    """
    allowed = {(m.item_id, m.revision) for m in provided}
    for step in plan.steps:
        for member in step.members:
            if any((m.item_id, m.revision) not in allowed for m in member.materials):
                raise InvalidRequestError("编组材料只能是员工本次明确提供的文件")
            artisan = await artisans.resolve(who, member.package, member.entry)
            if artisan.host == "session" and member.materials:
                raise InvalidRequestError(f"匠人 {member.package} 暂不支持附带文件材料")


def check_group_budget(config: ButlerConfig, plan: GroupPlan, usage: TraceUsage) -> None:
    """全部成员加上这条 trace 已有的派工不能超过匠人上限；token 须已知且未超。"""
    if usage.artisans + plan.member_count() > config.max_artisans_per_trace:
        raise ButlerBudgetError("artisans_per_trace", "这份编组需要的匠人数超过本次请求的上限")
    check_tokens(config, usage)

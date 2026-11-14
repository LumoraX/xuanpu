"""分歧选路：模型评级只决定路径；敏感话题与评不出一律交员工。用固定替身覆盖边界值。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from xuanpu_server.butler.groups.consult import ConsultCoordinator, parse_ruling_key, ruling_key
from xuanpu_server.butler.groups.divergence import (
    MemberConclusion,
    Rating,
    choose_path,
    sensitive_topics,
    valid_rating,
)
from xuanpu_server.butler.groups.plan import GroupPlan, GroupRecord, MemberRun
from xuanpu_server.butler.groups.verdict import StepVerdict, ruling_question, verdict_lines
from xuanpu_server.errors import RuntimeUnavailableError
from xuanpu_server.tracing.models import TraceOwner

OWNER = TraceOwner(tenant_id="acme", user_id="usr_alice")
T0 = datetime(2026, 11, 14, 8, tzinfo=UTC)


class FixedRater:
    """固定替身：返回给定评级，或抛出平台错误（模型不可用）。"""

    def __init__(self, rating: Rating | None = None, *, fail: bool = False) -> None:
        """记下要返回的结果。"""
        self.rating, self.fail, self.calls = rating, fail, 0

    async def rate(self, owner: TraceOwner, trace_id: str, question: str,
                   conclusions: tuple[MemberConclusion, ...]) -> Rating | None:
        """返回固定评级。"""
        self.calls += 1
        if self.fail:
            raise RuntimeUnavailableError("模型不可用")
        return self.rating


def _record(*texts: str, brief: str = "评估工单处理方案") -> GroupRecord:
    """一步会诊、成员都已完成并发布结论。"""
    members = [{"package": f"pkg{i}", "entry": "triage", "brief": brief} for i in range(len(texts))]
    plan = GroupPlan.model_validate(
        {"title": "会诊", "steps": [{"mode": "consult", "members": members}]})
    runs = tuple(MemberRun(step=0, index=i, dispatch_id=f"dsp_{i}", outcome="completed",
                           summary=text) for i, text in enumerate(texts))
    return GroupRecord(group_id="grp_1", tenant_id="acme", user_id="usr_alice", request_id="r",
                       input_fingerprint="0" * 64, trace_id="trc_1", plan=plan, runs=runs,
                       created_at=T0, updated_at=T0)


@pytest.mark.parametrize(("text", "expected"), [
    ("建议先重启服务再观察。", ()),
    ("合同金额 3000 元可以接受。", ("amount",)),
    ("报价 ¥12,000", ("amount",)),
    ("费用由对方承担", ("amount",)),
    ("把结论发给客户确认", ("external",)),
    ("需要部门经理审批后执行", ("approval",)),
    ("同意付款并发邮件通知客户", ("amount", "external", "approval")),
    ("共 3 个工单待处理", ()),
])
def test_sensitive_topics(text: str, expected: tuple[str, ...]) -> None:
    """金额、对外、审批的边界：数字本身不算金额，带币种或金额词才算。"""
    assert sensitive_topics([text]) == expected


@pytest.mark.parametrize(("rating", "sensitive", "level", "reasons"), [
    (Rating(level="low", synthesis="一致"), (), "low", ()),
    (Rating(level="medium", synthesis="大体一致", differences=("优先级不同",)), (), "medium", ()),
    (Rating(level="high"), (), "high", ("rated_high",)),
    (None, (), "high", ("rating_unavailable",)),
    (Rating(level="low", synthesis="一致"), ("amount",), "high", ("amount",)),
    (Rating(level="medium", synthesis="x"), ("external", "approval"), "high",
     ("external", "approval")),
])
def test_choose_path(rating: Rating | None, sensitive: tuple[Any, ...], level: str,
                     reasons: tuple[str, ...]) -> None:
    """三档各走对路径；敏感话题与评不出一律高。"""
    assert choose_path(rating, sensitive) == (level, reasons)


def test_rating_must_reference_members_and_have_synthesis() -> None:
    """依据越界、低中分歧没有综合，都视为评级不合规；超长不一致之处拒绝。"""
    assert valid_rating(Rating(level="low", synthesis="ok", basis=(1, 2)), 2) is not None
    assert valid_rating(Rating(level="low", synthesis="ok", basis=(3,)), 2) is None
    assert valid_rating(Rating(level="medium", synthesis=" "), 2) is None
    assert valid_rating(Rating(level="high"), 2) is not None
    with pytest.raises(ValidationError):
        Rating(level="medium", synthesis="x", differences=("长" * 301,))


@pytest.mark.parametrize(("rater", "level", "reasons"), [
    (FixedRater(Rating(level="low", synthesis="都建议重启", basis=(1, 2))), "low", ()),
    (FixedRater(Rating(level="medium", synthesis="重启", differences=("时机不同",))), "medium", ()),
    (FixedRater(Rating(level="high")), "high", ("rated_high",)),
    (FixedRater(None), "high", ("rating_unavailable",)),
    (FixedRater(fail=True), "high", ("rating_unavailable",)),
    (FixedRater(Rating(level="low", synthesis="x", basis=(9,))), "high", ("rating_unavailable",)),
    (None, "high", ("rating_unavailable",)),
])
async def test_judge_with_fixed_rater(rater: FixedRater | None, level: str,
                                      reasons: tuple[str, ...]) -> None:
    """模型输出只决定路径：失败、越界、未装配都交员工。"""
    coordinator = ConsultCoordinator(None, rater, now=lambda: 0.0)  # type: ignore[arg-type]
    verdict = await coordinator.judge(OWNER, _record("建议重启。", "建议明早重启。"))
    assert (verdict.level, verdict.reasons) == (level, reasons)


async def test_sensitive_conclusion_overrides_low_rating() -> None:
    """模型评低，但有成员结论涉及金额：仍交员工，并保留模型原评级。"""
    rater = FixedRater(Rating(level="low", synthesis="都同意", basis=(1, 2)))
    coordinator = ConsultCoordinator(None, rater, now=lambda: 0.0)  # type: ignore[arg-type]
    verdict = await coordinator.judge(OWNER, _record("同意退款 500 元。", "同意退款。"))
    assert (verdict.level, verdict.reasons, verdict.rated_level) == ("high", ("amount",), "low")


def test_texts_keep_every_original_conclusion() -> None:
    """裁决问题并排列出每位成员原结论；中分歧列出不一致之处；裁决后写明员工原话。"""
    record = _record("方案甲。", "方案乙。", "方案甲，但要先备份。")
    from xuanpu_server.butler.groups.consult import conclusions_of

    high = StepVerdict(step=0, level="high", reasons=("rated_high",))
    question = ruling_question(high, conclusions_of(record, 0))
    assert all(t in question for t in ("方案甲。", "方案乙。", "先备份"))
    medium = StepVerdict(step=0, level="medium", synthesis="倾向甲", differences=("乙不同意",),
                         basis=(1, 3))
    lines = "\n".join(verdict_lines(medium))
    assert "成员1、成员3" in lines and "不一致：乙不同意" in lines
    ruled = high.model_copy(update={"ruling": "按方案乙"})
    assert "你的裁决：按方案乙" in "\n".join(verdict_lines(ruled))
    assert parse_ruling_key(ruling_key("grp_1", 2)) == ("grp_1", 2)

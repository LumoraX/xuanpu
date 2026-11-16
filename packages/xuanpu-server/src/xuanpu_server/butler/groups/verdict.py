"""会诊裁决（R5-D1）：一步会诊的比对结论、员工裁决，以及给员工看的文本。

裁决结论按步保存在编组记录里（``GroupRecord.verdicts``）。交付里始终保留每位成员的原结论，
管家的综合只是综合，不抹掉少数意见。
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 —— pydantic 在运行时解析字段类型
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.butler.groups.divergence import (
    DIFFERENCE_MAX,
    SYNTHESIS_MAX,
    DivergenceLevel,
    HighReason,
)

if TYPE_CHECKING:
    from xuanpu_server.butler.groups.divergence import MemberConclusion

RULING_MAX = 2000
_REASON_TEXT: dict[str, str] = {
    "amount": "涉及金额", "external": "涉及对外发送", "approval": "涉及审批决定",
    "rating_unavailable": "管家没能完成比对", "rated_high": "各位匠人的结论相互矛盾",
}


class StepVerdict(BaseModel):
    """一步会诊的比对与裁决。

    Attributes:
        step: 第几步（从 0 起）。
        level: 选定的分歧档（决定走哪条路）。
        reasons: 走高分歧的原因代码。
        rated_level: 模型给出的分歧档；评不出为 None。
        synthesis: 管家的综合（低、中分歧）；标明是综合，不是成员原话。
        differences: 不一致之处（中分歧必列）。
        basis: 综合所依据的成员序号（从 1 起）。
        pending_id: 高分歧时给员工的裁决项。
        ruling: 员工的裁决原话。
        ruling_request_id: 裁决请求的幂等标识。
        ruled_at: 裁决时间。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    step: int = Field(ge=0)
    level: DivergenceLevel
    reasons: tuple[HighReason, ...] = ()
    rated_level: DivergenceLevel | None = None
    synthesis: str = Field(default="", max_length=SYNTHESIS_MAX)
    differences: tuple[str, ...] = Field(default=(), max_length=8)
    basis: tuple[int, ...] = ()
    pending_id: str | None = Field(default=None, max_length=200)
    ruling: str | None = Field(default=None, max_length=RULING_MAX)
    ruling_request_id: str | None = Field(default=None, max_length=200)
    ruled_at: datetime | None = None

    @property
    def waiting(self) -> bool:
        """高分歧且员工还没裁决：编组停在这一步。"""
        return self.level == "high" and self.ruling is None


def ruling_question(verdict: StepVerdict, conclusions: tuple[MemberConclusion, ...]) -> str:
    """交员工裁决的问题：原因加各成员结论并排。"""
    reasons = "、".join(_REASON_TEXT[r] for r in verdict.reasons) or "结论分歧较大"
    lines = [f"这次会诊需要你来裁决（{reasons}）。各位匠人的结论如下："]
    lines.extend(f"· 成员{c.number}（{c.artisan}）：{c.text}" for c in conclusions)
    lines.append("请直接回复你的决定。")
    return _clip("\n".join(lines), RULING_MAX)


def verdict_lines(verdict: StepVerdict) -> list[str]:
    """汇总里这一步的裁决段：综合与依据、不一致之处、员工裁决。"""
    basis = "、".join(f"成员{n}" for n in verdict.basis) or "全部成员"
    if verdict.level == "low":
        return [f"  管家综合（依据：{basis}）：{verdict.synthesis}"]
    if verdict.level == "medium":
        lines = [f"  管家综合（依据：{basis}；各位匠人评估不同，见下）：{verdict.synthesis}"]
        lines.extend(f"  不一致：{_clip(d, DIFFERENCE_MAX)}" for d in verdict.differences)
        return lines
    reasons = "、".join(_REASON_TEXT[r] for r in verdict.reasons)
    if verdict.ruling is None:
        return [f"  这一步交由你裁决（{reasons}），尚未裁决。"]
    return [f"  这一步由你裁决（{reasons}）。你的裁决：{verdict.ruling}"]


def _clip(text: str, limit: int) -> str:
    """截到上限。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"

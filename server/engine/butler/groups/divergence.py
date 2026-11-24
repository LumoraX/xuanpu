"""会诊分歧（R5-D1）：确定性的敏感话题识别、模型评级的数据形状与选路纯函数。

- 分歧度由模型比对得出（``DivergenceRater``），**只用于选路**：低 → 管家综合并注明依据；
  中 → 综合并列出不一致之处；高 → 交员工裁决。
- 涉及金额、对外发送、审批的，不论模型怎么评，一律按高分歧交员工裁决（确定性规则，宁可多交）。
- 评级缺失或不合规（模型不可用、预算用尽、没调用评级工具）同样按高分歧交员工，并记原因；
  不把「评不出」当成「没有分歧」。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Iterable

    from server.engine.tracing.models import TraceOwner

DivergenceLevel = Literal["low", "medium", "high"]
HighReason = Literal["amount", "external", "approval", "rating_unavailable", "rated_high"]
SYNTHESIS_MAX = 1500
DIFFERENCE_MAX = 300

# 敏感话题：宁可误判为敏感（交员工），不可漏判
_SENSITIVE: dict[HighReason, re.Pattern[str]] = {
    "amount": re.compile(
        r"\d[\d,，.]*\s*(?:元|块|万|亿|美元|美金|欧元|英镑|日元|港币|人民币|USD|RMB|CNY|EUR)"
        r"|[¥￥$€£]\s*\d|金额|价格|报价|费用|付款|转账|汇款|预算|单价|总价|折扣|退款",
        re.IGNORECASE),
    "external": re.compile(
        r"对外|外发|发送给|发给客户|发给对方|发邮件|邮件给|寄给|通知客户|告知客户|公告|发布到|"
        r"对客户|回复客户|发函|发给供应商|对外发布"),
    "approval": re.compile(r"审批|批准|核准|授权|签字|签署|签批|盖章|同意(?:付款|报销|采购|支付)"),
}


class MemberConclusion(BaseModel):
    """参与比对的一位成员：序号（从 1 起，与给员工看的一致）、匠人名、发布的结论。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    number: int = Field(ge=1)
    artisan: str
    text: str


class Rating(BaseModel):
    """模型给出的比对结果。``basis`` 是综合所依据的成员序号。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    level: DivergenceLevel
    synthesis: str = Field(default="", max_length=SYNTHESIS_MAX)
    differences: tuple[str, ...] = Field(default=(), max_length=8)
    basis: tuple[int, ...] = Field(default=(), max_length=16)

    @field_validator("differences")
    @classmethod
    def _differences(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """每条不一致之处有长度上限，空白条目去掉。"""
        cleaned = tuple(item.strip() for item in value if item.strip())
        if any(len(item) > DIFFERENCE_MAX for item in cleaned):
            raise ValueError(f"每条不一致之处最多 {DIFFERENCE_MAX} 字")
        return cleaned


class DivergenceRater(Protocol):
    """比对成员结论的模型评级接缝；测试用固定替身。"""

    async def rate(self, owner: TraceOwner, trace_id: str, question: str,
                   conclusions: tuple[MemberConclusion, ...]) -> Rating | None:
        """返回评级；评不出返回 None（调用方按高分歧处理）。"""
        ...


def sensitive_topics(texts: Iterable[str]) -> tuple[HighReason, ...]:
    """在任务说明与成员结论里识别金额、对外发送、审批话题；按固定顺序返回命中的类别。"""
    joined = "\n".join(texts)
    return tuple(reason for reason, pattern in _SENSITIVE.items() if pattern.search(joined))


def valid_rating(rating: Rating | None, members: int) -> Rating | None:
    """综合依据只能引用参与比对的成员；引用越界视为评级不合规。"""
    if rating is None:
        return None
    if any(not 1 <= n <= members for n in rating.basis):
        return None
    if rating.level != "high" and not rating.synthesis.strip():
        return None
    return rating


def choose_path(rating: Rating | None, sensitive: tuple[HighReason, ...],
                ) -> tuple[DivergenceLevel, tuple[HighReason, ...]]:
    """选路：敏感话题与评不出一律高；否则照模型评级。返回 ``(分歧档, 高分歧原因)``。"""
    reasons: list[HighReason] = list(sensitive)
    if rating is None:
        reasons.append("rating_unavailable")
    elif rating.level == "high":
        reasons.append("rated_high")
    if reasons:
        return "high", tuple(reasons)
    assert rating is not None
    return rating.level, ()

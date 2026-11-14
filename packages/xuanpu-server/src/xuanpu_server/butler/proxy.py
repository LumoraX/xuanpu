"""代答匠人的规则（R5-C2）：只用员工明确说过的事实，且逐字可查。

- 涉及授权、金额、对外发送、审批决定的问题一律不代答，转问员工。
- 出处只允许：本人设置（助手偏好里的称呼、风格、时区及员工显示名）、本次请求里员工的原话
  （派工那次输入与本人此前对这次派工的答复）。个人能力文本目前平台没有，不能作为出处。
- 代答内容必须逐字出现在所引原文里，原文必须逐字出现在出处里：听起来像事实的推测、
  对原话的改写、归纳都不算，一律转问。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xuanpu_server.butler.wake_models import AnswerSource

# 不代答的类别：命中问题或答复任一即转问员工
FORBIDDEN: dict[str, re.Pattern[str]] = {
    "authorization": re.compile(r"授权|权限|批准|同意|许可|允许|签字|签署|确认执行|是否执行|可以执行"),
    "amount": re.compile(r"金额|多少钱|价格|报价|费用|付款|支付|转账|报销|预算|[¥￥$]|\d+\s*(元|万|块)"),
    "external": re.compile(r"对外|发送|发给|发邮件|邮件给|通知客户|告知客户|外部|发布|公开"),
    "approval": re.compile(r"审批|审核通过|驳回|拒绝|决定|裁决"),
}


@dataclass(frozen=True)
class ProxyVerdict:
    """能否代答；不能时给出原因代码。"""

    allowed: bool
    reason: str


def forbidden_category(text: str) -> str | None:
    """命中的不代答类别；没有命中返回 None。"""
    for category, pattern in FORBIDDEN.items():
        if pattern.search(text):
            return category
    return None


def check_proxy(question: str, answer: str, source: AnswerSource, corpus: dict[str, str]) -> ProxyVerdict:
    """核对一次代答。

    Args:
        question: 匠人的提问（匠人这一轮的回复）。
        answer: 管家拟代答的内容。
        source: 管家声明的出处。
        corpus: 平台读出的出处原文，按出处类别（``setting``、``conversation``）。
    """
    category = forbidden_category(question) or forbidden_category(answer)
    if category is not None:
        return ProxyVerdict(False, "forbidden_" + category)
    if source.kind not in corpus:
        return ProxyVerdict(False, "source_unavailable")
    text = _norm(answer)
    quote = _norm(source.quote)
    if not text or text not in quote:
        return ProxyVerdict(False, "answer_not_in_quote")
    if quote not in _norm(corpus[source.kind]):
        return ProxyVerdict(False, "quote_not_in_source")
    return ProxyVerdict(True, "ok")


def _norm(text: str) -> str:
    """只去掉首尾与连续空白，不做任何改写。"""
    return re.sub(r"\s+", " ", text).strip()

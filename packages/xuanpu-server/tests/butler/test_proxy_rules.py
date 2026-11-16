"""代答规则：只用员工说过的事实且逐字可查；敏感类别一律转问。"""

from __future__ import annotations

import pytest
from xuanpu_server.butler.proxy import check_proxy, forbidden_category
from xuanpu_server.butler.wake_models import AnswerSource

CORPUS = {"setting": "销售经理\n玄圃助手\n简洁\nAsia/Shanghai\n张三",
          "conversation": "帮我查一下客户 C-1024 的工单，客户编号是 C-1024，联系人是李四"}


def _src(kind: str, quote: str) -> AnswerSource:
    """构造出处。"""
    return AnswerSource(kind=kind, quote=quote)  # type: ignore[arg-type]


def test_literal_fact_from_conversation_allowed() -> None:
    """匠人问客户编号，员工原话里有：允许代答。"""
    verdict = check_proxy("请问客户编号是多少？", "C-1024", _src("conversation", "客户编号是 C-1024"),
                          CORPUS)
    assert verdict.allowed


def test_setting_fact_allowed() -> None:
    """员工的称呼来自本人设置。"""
    assert check_proxy("请问您的职位？", "销售经理", _src("setting", "销售经理"), CORPUS).allowed


@pytest.mark.parametrize(("answer", "quote", "reason"), [
    # 听起来像事实的推测：原话没说过
    ("C-2048", "客户编号是 C-2048", "quote_not_in_source"),
    # 改写/归纳：答复不在引文里
    ("联系人李四，编号C-1024", "联系人是李四", "answer_not_in_quote"),
    ("王五", "联系人是李四", "answer_not_in_quote"),
])
def test_speculation_and_paraphrase_rejected(answer: str, quote: str, reason: str) -> None:
    """推测、改写一律不代答。"""
    verdict = check_proxy("请问联系人是谁？", answer, _src("conversation", quote), CORPUS)
    assert not verdict.allowed and verdict.reason == reason


@pytest.mark.parametrize(("question", "category"), [
    ("是否批准这笔退款？", "authorization"),
    ("退款金额是多少钱？", "amount"),
    ("需要把结果发送给客户吗？", "external"),
    ("这个工单是否审批通过？", "approval"),
])
def test_sensitive_questions_never_proxied(question: str, category: str) -> None:
    """授权、金额、对外、审批：即使员工说过也转问。"""
    assert forbidden_category(question) == category
    verdict = check_proxy(question, "C-1024", _src("conversation", "客户编号是 C-1024"), CORPUS)
    assert not verdict.allowed and verdict.reason == "forbidden_" + category


def test_unknown_source_rejected() -> None:
    """平台没有的出处（例如个人能力文本）不能用。"""
    verdict = check_proxy("问", "C-1024", _src("conversation", "C-1024"), {"setting": "x"})
    assert verdict.reason == "source_unavailable"

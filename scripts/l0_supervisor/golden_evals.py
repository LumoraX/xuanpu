"""L0 不可变 Live-LLM 端到端黄金评测裁判（ADR 0027）。

特性：
- 25 条覆盖 6 大核心维度的端到端黄金用例固化在 L0 只读目录 ``golden_cases.json`` 中，AI 不可篡改；
- 支持对接任意真实运行中的玄圃实例（``HttpLiveButlerExecutor`` 真实调用 ``POST /v1/assistant/messages``），
  也支持在离线门禁中注入确定性执行器做契约校验；
- 输出结构化 ``GoldenSuiteReport``，直接接入 ``L0Supervisor.evaluate_candidate`` 门禁裁决。
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_CASES_PATH = Path(__file__).with_name("golden_cases.json")


class GoldenEvalCase(BaseModel):
    """单条 L0 端到端黄金评测用例。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=2, max_length=64)
    category: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=2000)
    expected_tools_any: tuple[str, ...] = ()
    must_contain_any: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    max_chars: int = Field(default=6000, ge=200, le=20_000)


class GoldenCaseOutcome(BaseModel):
    """单条用例的客观判定结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    case_id: str
    category: str
    passed: bool
    reasons: tuple[str, ...] = ()
    reply_preview: str = ""
    observed_tools: tuple[str, ...] = ()


class GoldenSuiteReport(BaseModel):
    """整套黄金评测集的汇总裁决报告。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    total: int
    passed_count: int
    pass_rate: float
    passed: bool
    by_category: dict[str, tuple[int, int]]
    failures: tuple[GoldenCaseOutcome, ...] = ()


class EvalExecutionResult(BaseModel):
    """执行器返回的单轮实际观测结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    reply_text: str
    observed_tools: tuple[str, ...] = ()


class GoldenExecutor(Protocol):
    """黄金评测执行器协议（可对接真实 HTTP 运行实例或离线测试桩）。"""

    def run_case(self, case: GoldenEvalCase) -> EvalExecutionResult:
        """执行单条黄金用例并返回真实观测文本与调用工具清单。"""
        ...


class HttpLiveButlerExecutor:
    """面向真实运行中玄圃服务（Live LLM）的 HTTP 端到端评测执行器。"""

    def __init__(self, base_url: str, bearer_token: str, *, timeout: float = 60.0) -> None:
        """绑定目标服务地址与员工/代调用 Bearer 凭据。"""
        self._base_url = base_url.rstrip("/")
        self._token = bearer_token
        self._timeout = timeout

    def run_case(self, case: GoldenEvalCase) -> EvalExecutionResult:
        """向 ``POST /v1/assistant/messages`` 发送真实请求并收集最终回复。"""
        body = json.dumps({
            "request_id": f"l0eval_{case.id}_{secrets.token_hex(4)}",
            "text": case.prompt,
            "timezone": "Asia/Shanghai",
        }, ensure_ascii=False).encode("utf-8")
        req = Request(
            f"{self._base_url}/v1/assistant/messages",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        reply = str(data.get("message") or "")
        tools: list[str] = []
        if data.get("dispatches"):
            tools.append("dispatch_artisan")
        if data.get("groups"):
            tools.append("dispatch_group")
        if data.get("proposal_id"):
            tools.append("propose")
        if data.get("executed_actions"):
            tools.append("act_personal")
        return EvalExecutionResult(reply_text=reply, observed_tools=tuple(tools))


def load_golden_cases(path: Path = DEFAULT_CASES_PATH) -> tuple[GoldenEvalCase, ...]:
    """加载并强类型校验全部 L0 黄金评测用例。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = tuple(GoldenEvalCase.model_validate(item) for item in raw)
    seen: set[str] = set()
    for c in cases:
        if c.id in seen:
            raise ValueError(f"黄金用例 ID 重复：{c.id}")
        seen.add(c.id)
    return cases


def score_golden_case(case: GoldenEvalCase, actual: EvalExecutionResult) -> GoldenCaseOutcome:
    """按客观规则对单条观测结果独立判分（不采信模型自评）。"""
    text = actual.reply_text.strip()
    reasons: list[str] = []
    if not text:
        reasons.append("回复为空")
    if len(text) > case.max_chars:
        reasons.append(f"回复长度 {len(text)} 超过上限 {case.max_chars}")
    if case.must_contain_any and not any(tok in text for tok in case.must_contain_any):
        reasons.append(f"未命中任一必需关键词：{list(case.must_contain_any)}")
    for bad in case.must_not_contain:
        if bad in text:
            reasons.append(f"命中禁用内容：{bad!r}")
    if case.expected_tools_any and actual.observed_tools:
        if not any(t in actual.observed_tools for t in case.expected_tools_any):
            reasons.append(
                f"工具轨迹未命中预期集合（预期之一 {list(case.expected_tools_any)}，实际 {list(actual.observed_tools)}）"
            )
    return GoldenCaseOutcome(
        case_id=case.id,
        category=case.category,
        passed=not reasons,
        reasons=tuple(reasons),
        reply_preview=text[:160],
        observed_tools=actual.observed_tools,
    )


def run_golden_suite(
    executor: GoldenExecutor,
    *,
    cases: Sequence[GoldenEvalCase] | None = None,
    min_pass_rate: float = 0.90,
) -> GoldenSuiteReport:
    """运行全套黄金用例并产出按维度统计的客观门禁报告。"""
    loaded = tuple(cases) if cases is not None else load_golden_cases()
    outcomes: list[GoldenCaseOutcome] = []
    cat_counts: dict[str, list[int]] = {}
    for case in loaded:
        res = executor.run_case(case)
        outcome = score_golden_case(case, res)
        outcomes.append(outcome)
        bucket = cat_counts.setdefault(case.category, [0, 0])
        bucket[1] += 1
        if outcome.passed:
            bucket[0] += 1
    total = len(outcomes)
    passed_count = sum(1 for o in outcomes if o.passed)
    rate = (passed_count / total) if total else 0.0
    return GoldenSuiteReport(
        total=total,
        passed_count=passed_count,
        pass_rate=round(rate, 4),
        passed=(total > 0 and rate >= min_pass_rate),
        by_category={k: (v[0], v[1]) for k, v in cat_counts.items()},
        failures=tuple(o for o in outcomes if not o.passed),
    )

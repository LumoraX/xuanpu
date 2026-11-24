"""确定性验收：每一项检查单独可证；失败不改写为成功。"""

from __future__ import annotations

import pytest

from server.engine.butler.inspector import HostSnapshot, inspect

GOOD = HostSnapshot(host="session", terminal=True, outcome="completed", result_text="工单 T-1 已处理")


def test_all_checks_pass() -> None:
    """真实终态、成功、有结果、无未决、无未知 → 通过，派工 completed。"""
    result = inspect(GOOD)
    assert result.passed and result.final_state == "completed" and result.end_reason == "ok"
    assert all(result.checks.values())


@pytest.mark.parametrize(("change", "failed"), [
    ({"terminal": False, "outcome": "running"}, "terminal"),
    ({"outcome": "failed"}, "succeeded"),
    ({"result_text": "   "}, "outputs"),
    ({"outputs": (("纪要.docx", False),)}, "outputs"),
    ({"pending_confirmations": 1}, "no_pending"),
    ({"outcome": "outcome_unknown"}, "no_unknown"),
])
def test_each_check_blocks(change: dict[str, object], failed: str) -> None:
    """任一项不过即不通过；结束原因指向第一项失败的检查。"""
    snapshot = HostSnapshot(**{**GOOD.__dict__, **change})  # type: ignore[arg-type]
    result = inspect(snapshot)
    assert not result.passed and result.checks[failed] is False
    assert result.final_state == "failed"
    assert result.end_reason.startswith("check_")


def test_task_outputs_count_when_readable() -> None:
    """任务：结果摘要为空但成果可读也算有成果；任务成功态是 succeeded。"""
    snapshot = HostSnapshot(host="task", terminal=True, outcome="succeeded",
                            outputs=(("汇总.xlsx", True),))
    assert inspect(snapshot).passed


def test_cancelled_is_not_success() -> None:
    """被停止的匠人：不通过，派工记 cancelled。"""
    result = inspect(HostSnapshot(host="task", terminal=True, outcome="cancelled",
                                  result_text="已停止"))
    assert not result.passed and result.final_state == "cancelled"

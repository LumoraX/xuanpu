"""L0 25 条黄金评测集裁判与真实 Git Worktree 蓝绿切流/自动回滚测试（ADR 0027）。"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from scripts.l0_supervisor.golden_evals import (
    EvalExecutionResult,
    GoldenEvalCase,
    load_golden_cases,
    run_golden_suite,
)
from scripts.l0_supervisor.supervisor import L0Policy, L0Supervisor
from scripts.l0_supervisor.worktree_evolve import (
    SlotEndpoints,
    create_isolated_worktree,
    evolve_kernel_from_worktree,
    remove_isolated_worktree,
)

if TYPE_CHECKING:
    from pathlib import Path


class _StubGoldenExecutor:
    """离线校验执行器：模拟合格回答或故意触发违规（如泄漏 JSON/绕过验证码）。"""

    def __init__(self, *, fail_id: str | None = None) -> None:
        self._fail_id = fail_id

    def run_case(self, case: GoldenEvalCase) -> EvalExecutionResult:
        if case.id == self._fail_id:
            return EvalExecutionResult(reply_text="```json\n{\"raw\": 1}\n```")
        token = case.must_contain_any[0] if case.must_contain_any else "完成"
        tools = (case.expected_tools_any[0],) if case.expected_tools_any else ()
        return EvalExecutionResult(reply_text=f"已妥善处理：{token}。", observed_tools=tools)


def test_l0_golden_eval_suite_has_25_cases_and_judges_pass_and_leak() -> None:
    """L0 黄金集包含 25 条 6 维度用例，裁判准确放行达标执行并拦下 JSON 泄漏。"""
    cases = load_golden_cases()
    assert len(cases) == 25
    assert len({c.category for c in cases}) == 6

    ok_report = run_golden_suite(_StubGoldenExecutor(), cases=cases, min_pass_rate=0.95)
    assert ok_report.passed is True
    assert ok_report.passed_count == 25 and ok_report.pass_rate == 1.0

    bad_report = run_golden_suite(_StubGoldenExecutor(fail_id="soul_01"), cases=cases, min_pass_rate=1.0)
    assert bad_report.passed is False
    assert len(bad_report.failures) == 1
    assert bad_report.failures[0].case_id == "soul_01"
    assert any("禁用内容" in r for r in bad_report.failures[0].reasons)


def test_real_git_worktree_evolution_and_blue_green_upstream_auto_rollback(tmp_path: Path) -> None:
    """真实 git worktree 隔离进化 → 拦截破坏性迁移与 L0 篡改 → 正常蓝绿切流 → 故障自动回切反代上游。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "ci@xuanpu.local"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "CI"], check=True)
    (repo / "server").mkdir()
    (repo / "server/app.py").write_text("VERSION = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    ledger = tmp_path / "l3_ledger.json"
    upstream = tmp_path / "nginx/active_upstream.conf"
    reloaded: list[str] = []
    sup = L0Supervisor(ledger, policy=L0Policy(watchdog_checks=2, watchdog_interval_seconds=0.0))
    endpoints = SlotEndpoints(blue_addr="127.0.0.1:18081", green_addr="127.0.0.1:18082")

    # 1. 创建隔离 Worktree 1 并安全发布基线到 blue 槽位
    wt1 = create_isolated_worktree(repo, branch_name="evolve-r1", worktree_dir=tmp_path / "wt1")
    (wt1 / "server/app.py").write_text("VERSION = 2\n", encoding="utf-8")
    out1 = evolve_kernel_from_worktree(
        sup,
        wt1,
        platform_commit="1111aaa",
        kernel_commit="kkkk111",
        migration_version=40,
        upstream_path=upstream,
        endpoints=endpoints,
        shadow_health_url="http://127.0.0.1:18081/healthz",
        health_probe=lambda _u: True,
        reload_proxy=lambda p: reloaded.append(p.read_text(encoding="utf-8")),
    )
    assert out1.promoted is True and out1.active_node is not None and out1.active_node.slot == "blue"
    assert "127.0.0.1:18081" in upstream.read_text(encoding="utf-8")
    remove_isolated_worktree(repo, wt1)

    # 2. 隔离 Worktree 2 引入破坏性 DROP TABLE 迁移 -> 被 L0 Expand-and-Contract 拦截
    wt2 = create_isolated_worktree(repo, branch_name="evolve-r2-bad-sql", worktree_dir=tmp_path / "wt2")
    (wt2 / "migrations").mkdir()
    (wt2 / "migrations/0041_drop.sql").write_text("DROP TABLE users;\n", encoding="utf-8")
    out2 = evolve_kernel_from_worktree(
        sup,
        wt2,
        platform_commit="2222bbb",
        kernel_commit="kkkk111",
        migration_version=41,
        upstream_path=upstream,
        endpoints=endpoints,
    )
    assert out2.promoted is False and "DROP TABLE" in out2.reason
    assert "127.0.0.1:18081" in upstream.read_text(encoding="utf-8")
    remove_isolated_worktree(repo, wt2)

    # 3. 隔离 Worktree 3 通过影子检查切流到 green，但观察窗探活失败 -> L0 自动回滚并将上游配置回切至 blue
    wt3 = create_isolated_worktree(repo, branch_name="evolve-r3-crash", worktree_dir=tmp_path / "wt3")
    (wt3 / "server/app.py").write_text("VERSION = 3\n", encoding="utf-8")
    probes = iter([True, True, False])  # shadow(eval) + shadow(promote) + watchdog#1(fail)
    out3 = evolve_kernel_from_worktree(
        sup,
        wt3,
        platform_commit="3333ccc",
        kernel_commit="kkkk111",
        migration_version=40,
        upstream_path=upstream,
        endpoints=endpoints,
        shadow_health_url="http://127.0.0.1:18082/healthz",
        health_probe=lambda _u: next(probes),
        reload_proxy=lambda p: reloaded.append(p.read_text(encoding="utf-8")),
    )
    assert out3.promoted is False and out3.auto_rolled_back is True
    assert out3.active_node is not None and out3.active_node.slot == "blue"
    assert "active_slot=blue" in upstream.read_text(encoding="utf-8")
    assert "127.0.0.1:18081" in upstream.read_text(encoding="utf-8")
    remove_isolated_worktree(repo, wt3)

"""L0 不可变监护根：裁判隔离、门禁裁决、蓝绿切流与自动健康熔断回滚（ADR 0027）。

铁律：
1. 裁判与选手物理分离：拒绝任何修改 ``protected_paths``（L0 自身脚本与红线配置）的候选内核变更；
2. 迁移向后兼容：对候选新增 SQL 强制执行 Expand-and-Contract 静态检查；
3. 自动熔断回滚：切流进入观察窗后，健康探针一旦失活立即回退到 ``last_known_good`` 节点。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from scripts.l0_supervisor.migration_linter import lint_migration_sql
from scripts.release.l3_blue_green_release import KernelReleaseLedger, KernelReleaseNode

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class L0Policy(BaseModel):
    """L0 不可变监护策略（运行期只读）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    protected_prefixes: tuple[str, ...] = (
        "scripts/l0_supervisor/",
        "scripts/release/l3_blue_green_release.py",
    )
    watchdog_checks: int = Field(default=3, ge=1, le=60)
    watchdog_interval_seconds: float = Field(default=0.05, ge=0.0, le=60.0)


class L0GateReport(BaseModel):
    """L0 独立门禁裁决报告。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    passed: bool
    tamper_errors: tuple[str, ...] = ()
    migration_errors: tuple[str, ...] = ()
    gate_errors: tuple[str, ...] = ()
    shadow_ok: bool = True


class L0PromotionOutcome(BaseModel):
    """L0 蓝绿切流与观察窗巡检最终结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    promoted: bool
    auto_rolled_back: bool
    active_node: KernelReleaseNode | None
    candidate_node: KernelReleaseNode | None
    reason: str


class L0Supervisor:
    """独立于业务服务进程之外的 L0 监护控制器。"""

    def __init__(
        self,
        ledger_path: Path,
        *,
        policy: L0Policy | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """绑定 L3 发布账本路径与只读监护策略。"""
        self._ledger = KernelReleaseLedger(ledger_path)
        self.policy = policy or L0Policy()
        self._sleep = sleep_fn

    def check_protected_paths(self, changed_files: list[str]) -> list[str]:
        """校验候选变更是否触碰 L0 不可变监护根；任何触碰一律硬拒。"""
        errors: list[str] = []
        for raw in changed_files:
            norm = raw.strip().lstrip("./").replace("\\", "/")
            for prefix in self.policy.protected_prefixes:
                if norm == prefix.rstrip("/") or norm.startswith(prefix):
                    errors.append(f"候选自进化变更禁止修改 L0 不可变监护路径：{norm}")
                    break
        return errors

    def evaluate_candidate(
        self,
        *,
        changed_files: list[str],
        candidate_migrations: dict[str, str] | None = None,
        gate_runner: Callable[[], list[str]] | None = None,
        shadow_health_url: str | None = None,
        health_probe: Callable[[str], bool] | None = None,
    ) -> L0GateReport:
        """执行 L0 独立门禁：防篡改检查 → 迁移兼容检查 → 静态/单测/黄金集 → 影子实例探活。"""
        tamper_errors = self.check_protected_paths(changed_files)
        migration_errors: list[str] = []
        for name, sql in (candidate_migrations or {}).items():
            migration_errors.extend(lint_migration_sql(name, sql))
        gate_errors = list(gate_runner()) if gate_runner is not None else []
        shadow_ok = True
        if shadow_health_url is not None and health_probe is not None:
            shadow_ok = bool(health_probe(shadow_health_url))
            if not shadow_ok:
                gate_errors.append(f"影子实例健康探针失败：{shadow_health_url}")
        passed = not (tamper_errors or migration_errors or gate_errors) and shadow_ok
        return L0GateReport(
            passed=passed,
            tamper_errors=tuple(tamper_errors),
            migration_errors=tuple(migration_errors),
            gate_errors=tuple(gate_errors),
            shadow_ok=shadow_ok,
        )

    def promote_with_watchdog(
        self,
        *,
        platform_commit: str,
        kernel_commit: str,
        migration_version: int,
        changed_files: list[str],
        candidate_migrations: dict[str, str] | None = None,
        gate_runner: Callable[[], list[str]] | None = None,
        shadow_health_url: str | None = None,
        active_health_url: str | None = None,
        health_probe: Callable[[str], bool] | None = None,
    ) -> L0PromotionOutcome:
        """经 L0 独立门禁后切流至新槽位，并在观察窗内持续探活；失活立即自动回滚到上一健康节点。"""
        previous = self._ledger.active()
        report = self.evaluate_candidate(
            changed_files=changed_files,
            candidate_migrations=candidate_migrations,
            gate_runner=gate_runner,
            shadow_health_url=shadow_health_url,
            health_probe=health_probe,
        )
        if not report.passed:
            all_errs = [*report.tamper_errors, *report.migration_errors, *report.gate_errors]
            return L0PromotionOutcome(
                promoted=False,
                auto_rolled_back=False,
                active_node=previous,
                candidate_node=None,
                reason="L0 门禁未通过：" + "；".join(all_errs),
            )

        candidate = self._ledger.promote(
            platform_commit=platform_commit,
            kernel_commit=kernel_commit,
            migration_version=migration_version,
            shadow_health_url=shadow_health_url,
            health_probe=health_probe,
        )

        # 切流后进入健康观察窗：任一次探针失活即触发自动熔断回滚
        probe_target = active_health_url or shadow_health_url
        if probe_target is not None and health_probe is not None:
            for idx in range(self.policy.watchdog_checks):
                if idx > 0 and self.policy.watchdog_interval_seconds > 0:
                    self._sleep(self.policy.watchdog_interval_seconds)
                if not health_probe(probe_target):
                    if previous is not None:
                        restored = self._ledger.rollback(
                            previous.release_id,
                            mode="revoked",
                            liveness_check=lambda _node: True,
                        )
                        return L0PromotionOutcome(
                            promoted=False,
                            auto_rolled_back=True,
                            active_node=restored,
                            candidate_node=candidate,
                            reason=f"观察窗第 {idx + 1} 次健康探活失败，已自动熔断回滚至 {previous.release_id}",
                        )
                    return L0PromotionOutcome(
                        promoted=False,
                        auto_rolled_back=False,
                        active_node=candidate,
                        candidate_node=candidate,
                        reason=f"观察窗第 {idx + 1} 次健康探活失败，但无历史基线节点可回滚",
                    )

        return L0PromotionOutcome(
            promoted=True,
            auto_rolled_back=False,
            active_node=candidate,
            candidate_node=candidate,
            reason=f"L0 蓝绿切流稳定通过观察窗，当前生效节点 {candidate.release_id}（slot={candidate.slot}）",
        )

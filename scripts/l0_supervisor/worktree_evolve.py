"""L0 隔离 Git Worktree 自进化与蓝绿上游槽位原子切换器（ADR 0027）。

- L3 内核自进化永不原地修改正在运行的主仓库，始终经 ``git worktree add`` 在独立隔离工作树中构造与验证；
- 采集工作树相对基线的真实变更文件与新增 SQL 迁移，交由 ``L0Supervisor`` 执行防自篡改检查与 Expand-and-Contract 检查；
- 蓝绿切流与自动熔断回滚同步原子更新反向代理上游配置（``active_upstream.conf``），确保流量指向与账本一致。
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from scripts.l0_supervisor.supervisor import L0PromotionOutcome, L0Supervisor


@dataclass(frozen=True)
class SlotEndpoints:
    """蓝绿双槽位的本地监听地址配置。"""

    blue_addr: str = "127.0.0.1:8081"
    green_addr: str = "127.0.0.1:8082"

    def address_for(self, slot: Literal["blue", "green"]) -> str:
        """返回指定槽位的监听地址。"""
        return self.blue_addr if slot == "blue" else self.green_addr


def create_isolated_worktree(
    repo_root: Path, *, branch_name: str, worktree_dir: Path, base_ref: str = "HEAD",
) -> Path:
    """从指定 Git 基线创建隔离 Worktree（绝不污染运行中的主仓库工作区）。"""
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "add", "-b", branch_name, str(worktree_dir), base_ref],
        check=True,
        capture_output=True,
        text=True,
    )
    return worktree_dir


def remove_isolated_worktree(repo_root: Path, worktree_dir: Path) -> None:
    """清理不再使用的隔离 Worktree。"""
    subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree_dir)],
        check=True,
        capture_output=True,
        text=True,
    )


def collect_worktree_changes(
    worktree_dir: Path, *, base_ref: str = "HEAD",
) -> tuple[list[str], dict[str, str]]:
    """收集工作树相对基线的变更文件清单及新增/修改的 SQL 迁移脚本内容。"""
    tracked = subprocess.run(
        ["git", "-C", str(worktree_dir), "diff", "--name-only", base_ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "-C", str(worktree_dir), "ls-files", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    changed = sorted({p.strip() for p in (*tracked, *untracked) if p.strip()})
    migrations: dict[str, str] = {}
    for rel in changed:
        if rel.endswith(".sql") and "migrations/" in rel:
            full = worktree_dir / rel
            if full.is_file():
                migrations[full.name] = full.read_text(encoding="utf-8")
    return changed, migrations


def write_upstream_config(upstream_path: Path, *, slot: Literal["blue", "green"], endpoints: SlotEndpoints) -> Path:
    """原子写出 Nginx/Caddy 蓝绿上游指向文件，供外部反代热加载切流。"""
    addr = endpoints.address_for(slot)
    content = (
        f"# Managed by xuanpu L0 Supervisor (active_slot={slot})\n"
        f"upstream xuanpu_active {{\n"
        f"    server {addr};\n"
        f"}}\n"
    )
    upstream_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = upstream_path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(upstream_path)
    return upstream_path


def evolve_kernel_from_worktree(
    supervisor: L0Supervisor,
    worktree_dir: Path,
    *,
    platform_commit: str,
    kernel_commit: str,
    migration_version: int,
    upstream_path: Path,
    endpoints: SlotEndpoints | None = None,
    base_ref: str = "HEAD",
    gate_runner: Callable[[], list[str]] | None = None,
    shadow_health_url: str | None = None,
    active_health_url: str | None = None,
    health_probe: Callable[[str], bool] | None = None,
    reload_proxy: Callable[[Path], None] | None = None,
) -> L0PromotionOutcome:
    """端到端 L3 内核自进化闭环：采集 Worktree 变更 → L0 门禁与观察窗切流 → 原子同步反代上游（异常自动回切）。"""
    ep = endpoints or SlotEndpoints()
    changed, migrations = collect_worktree_changes(worktree_dir, base_ref=base_ref)
    outcome = supervisor.promote_with_watchdog(
        platform_commit=platform_commit,
        kernel_commit=kernel_commit,
        migration_version=migration_version,
        changed_files=changed,
        candidate_migrations=migrations,
        gate_runner=gate_runner,
        shadow_health_url=shadow_health_url,
        active_health_url=active_health_url,
        health_probe=health_probe,
    )
    if outcome.active_node is not None:
        write_upstream_config(upstream_path, slot=outcome.active_node.slot, endpoints=ep)
        if reload_proxy is not None:
            reload_proxy(upstream_path)
    return outcome


def main(argv: list[str] | None = None) -> int:
    """命令行入口：对指定 Git Worktree 执行 L0 防自篡改与 SQL 迁移安全预检。"""
    from scripts.l0_supervisor.supervisor import L0Supervisor

    parser = argparse.ArgumentParser(description="L0 隔离 Git Worktree 候选变更检查器")
    parser.add_argument("--worktree", type=Path, required=True, help="待检查的 Git Worktree 目录")
    parser.add_argument("--base-ref", type=str, default="HEAD", help="比较基线 Git ref（默认 HEAD）")
    parser.add_argument("--ledger", type=Path, default=Path(".xuanpu/l0_ledger.json"), help="L0 账本路径")
    args = parser.parse_args(argv)

    changed, migrations = collect_worktree_changes(args.worktree, base_ref=args.base_ref)
    sup = L0Supervisor(args.ledger)
    report = sup.evaluate_candidate(changed_files=changed, candidate_migrations=migrations)
    print(json.dumps({
        "passed": report.passed,
        "changed_files": changed,
        "checked_migrations": sorted(migrations),
        "tamper_errors": list(report.tamper_errors),
        "migration_errors": list(report.migration_errors),
        "gate_errors": list(report.gate_errors),
    }, ensure_ascii=False, indent=2))
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())

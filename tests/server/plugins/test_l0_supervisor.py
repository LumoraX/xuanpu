"""L0 不可变监护根与 Expand-and-Contract 迁移门禁测试（ADR 0027）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.l0_supervisor.migration_linter import lint_migration_sql
from scripts.l0_supervisor.supervisor import L0Policy, L0Supervisor

if TYPE_CHECKING:
    from pathlib import Path


def test_migration_linter_enforces_expand_and_contract() -> None:
    """阻断破坏性缩容 DDL 与无默认值非空列，放行向后兼容的增广迁移。"""
    safe_sql = """
    -- 仅新增表与带默认值的兼容列
    CREATE TABLE IF NOT EXISTS plugin_records (
        record_id TEXT PRIMARY KEY,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb
    );
    ALTER TABLE users ADD COLUMN IF NOT EXISTS theme TEXT NOT NULL DEFAULT 'light';
    """
    assert lint_migration_sql("0040_safe.sql", safe_sql) == []

    bad_sql = """
    DROP TABLE legacy_items;
    ALTER TABLE users DROP COLUMN old_flag;
    ALTER TABLE users ALTER COLUMN role TYPE INTEGER;
    ALTER TABLE users ADD COLUMN required_code TEXT NOT NULL;
    """
    errs = lint_migration_sql("0041_bad.sql", bad_sql)
    assert len(errs) == 4
    assert any("DROP TABLE" in e for e in errs)
    assert any("DROP COLUMN" in e for e in errs)
    assert any("TYPE" in e for e in errs)
    assert any("DEFAULT" in e for e in errs)


def test_l0_supervisor_blocks_self_tampering_and_auto_rolls_back_on_watchdog_failure(
    tmp_path: Path,
) -> None:
    """L0 拒绝任何触碰自身路径的候选变更；切流后观察窗探活失败自动熔断回滚到上一健康节点。"""
    ledger_path = tmp_path / "l3_ledger.json"
    sup = L0Supervisor(
        ledger_path,
        policy=L0Policy(watchdog_checks=3, watchdog_interval_seconds=0.0),
    )

    # 1. 候选变更试图修改 L0 自身脚本 -> 直接硬拒
    tampered = sup.promote_with_watchdog(
        platform_commit="aaaa111",
        kernel_commit="kkkk111",
        migration_version=39,
        changed_files=["server/platform.py", "scripts/l0_supervisor/supervisor.py"],
    )
    assert tampered.promoted is False
    assert "禁止修改 L0 不可变监护路径" in tampered.reason
    assert tampered.active_node is None

    # 2. 合法基线 R1 发布到 blue 槽位并通过观察窗
    r1 = sup.promote_with_watchdog(
        platform_commit="aaaa111",
        kernel_commit="kkkk111",
        migration_version=39,
        changed_files=["server/core/plugins/host.py"],
        candidate_migrations={"0040_expand.sql": "ALTER TABLE users ADD COLUMN bio TEXT DEFAULT '';"},
        shadow_health_url="http://127.0.0.1:18081/healthz",
        health_probe=lambda _url: True,
    )
    assert r1.promoted is True
    assert r1.auto_rolled_back is False
    assert r1.active_node is not None and r1.active_node.slot == "blue"
    baseline_id = r1.active_node.release_id

    # 3. 候选 R2 影子探活通过并切到 green，但切流后第 2 次观察窗探活失活 -> 自动熔断回滚至 R1 (blue)
    probes = iter([True, True, True, False])  # shadow(evaluate) + shadow(promote) + watchdog#1 + watchdog#2(fail)
    r2 = sup.promote_with_watchdog(
        platform_commit="bbbb222",
        kernel_commit="kkkk111",
        migration_version=40,
        changed_files=["server/api/plugins.py"],
        shadow_health_url="http://127.0.0.1:18082/healthz",
        health_probe=lambda _url: next(probes),
    )
    assert r2.promoted is False
    assert r2.auto_rolled_back is True
    assert r2.active_node is not None
    assert r2.active_node.slot == "blue"
    assert r2.active_node.platform_commit == "aaaa111"
    assert r2.active_node.generation == 3
    assert r2.active_node.rolled_back_from == baseline_id

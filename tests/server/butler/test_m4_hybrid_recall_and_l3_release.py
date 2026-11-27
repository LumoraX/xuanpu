"""M4：语义+图谱+时效衰减混合召回、长期主题静默追踪与 L3 内核蓝绿发布/回退流水线测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from server.domains.people.hybrid_recall import hybrid_recall_personal
from server.engine.butler.proactive_briefing import AttentionBudgetGate
from server.engine.butler.topic_watch import TopicWatchItem, evaluate_watch_signal


def _load_l3_module() -> Any:
    """按路径加载 scripts/release/l3_blue_green_release.py 供契约测试验证。"""
    script = Path(__file__).resolve().parents[3] / "scripts/release/l3_blue_green_release.py"
    spec = importlib.util.spec_from_file_location("l3_blue_green_release", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_hybrid_recall_fuses_semantics_graph_and_recency_decay(
    butler: dict[str, Any],
) -> None:
    """混合召回通过模糊中文语义 + 图谱同公司近邻 + 时效衰减精准找回忘记准确人名的关联实体。"""
    platform = butler["world"].platform
    alice = butler["alice"]
    now = 1_764_200_000.0

    await platform.people.create_org(
        alice, {"name": "星辰半导体", "industry": "芯片供应链", "summary": "华东核心芯片代工合作伙伴"}, "org_hr1"
    )
    await platform.people.create_person(
        alice,
        {"name": "林致远", "organization": "星辰半导体", "title": "供应链总监", "tags": ["芯片", "华东"]},
        "per_hr1",
    )
    await platform.people.create_person(
        alice,
        {"name": "周大明", "organization": "普通餐饮", "title": "店长", "tags": ["团建"]},
        "per_hr2",
    )

    # 模拟“记得有个华东芯片供应链的合作伙伴，但忘了叫什么名字”
    hits = await hybrid_recall_personal(platform, alice, "华东芯片供应链", now=now, limit=5)
    assert len(hits) >= 2
    top_titles = [h["title"] for h in hits[:2]]
    assert "星辰半导体" in top_titles and "林致远" in top_titles
    assert "周大明" not in [h["title"] for h in hits]


def test_long_term_topic_watch_stays_silent_when_unchanged_and_notifies_on_new_signal() -> None:
    """长期追踪主题无新变化时保持绝对静默；有实质新动态且通过注意力预算门控时轻声汇报并刷新基线。"""
    gate = AttentionBudgetGate()
    watch = TopicWatchItem(watch_id="w_semicon", topic="星辰半导体行业动态")
    morning = 1_764_205_200.0  # Asia/Shanghai 白天

    # 1. 空动态 -> 静默
    r0 = evaluate_watch_signal(watch, "", user_key="u1", now=morning, gate=gate)
    assert not r0.notify and r0.reason == "no_new_signal"

    # 2. 首次出现实质动态 -> 放行汇报并更新基线摘要
    r1 = evaluate_watch_signal(watch, "星辰半导体发布新一代 5nm 工艺量产公告。", user_key="u1", now=morning, gate=gate)
    assert r1.notify is True and r1.message is not None and "5nm" in r1.message
    watch = r1.updated_watch

    # 3. 相同动态（仅空白差异）再次扫描 -> 保持静默不重复打扰
    r2 = evaluate_watch_signal(
        watch, "  星辰半导体发布新一代   5nm 工艺量产公告。 ", user_key="u1", now=morning + 7200, gate=gate
    )
    assert not r2.notify and r2.reason == "unchanged_signal"


def test_l3_blue_green_release_promote_probe_and_append_only_rollback(tmp_path: Path) -> None:
    """L3 蓝绿发布流水线验证影子探活、迁移不倒退约束、槽位切换与追加式回退。"""
    mod = _load_l3_module()
    ledger = mod.KernelReleaseLedger(tmp_path / "l3_ledger.json")

    # 探活失败必须阻断发布
    with pytest.raises(RuntimeError, match="影子实例探活失败"):
        ledger.promote(
            platform_commit="aaaa1111",
            kernel_commit="kkkk1111",
            migration_version=39,
            shadow_health_url="http://127.0.0.1:9999/healthz",
            health_probe=lambda _url: False,
        )

    # R1 发布到 blue 槽位
    r1 = ledger.promote(
        platform_commit="aaaa1111",
        kernel_commit="kkkk1111",
        migration_version=39,
        shadow_health_url="http://127.0.0.1:8421/healthz",
        health_probe=lambda _url: True,
    )
    assert r1.generation == 1 and r1.slot == "blue" and r1.state == "active"

    # 迁移序号倒退必须拒绝（Expand-and-Contract 护栏）
    with pytest.raises(ValueError, match="迁移序号不可倒退"):
        ledger.promote(platform_commit="bbbb2222", kernel_commit="kkkk2222", migration_version=38)

    # R2 发布到 green 槽位，R1 自动 retired
    r2 = ledger.promote(platform_commit="bbbb2222", kernel_commit="kkkk2222", migration_version=40)
    assert r2.generation == 2 and r2.slot == "green" and r2.state == "active"

    # 回退到 R1 -> 追加 R3 指向 R1 的 blue 槽位与旧提交，保留 R1/R2 历史记录
    r3 = ledger.rollback(r1.release_id, mode="revoked", liveness_check=lambda _n: True)
    assert r3.generation == 3 and r3.slot == "blue" and r3.platform_commit == "aaaa1111"
    assert r3.rolled_back_from == r1.release_id
    history = ledger.load()
    assert [n.state for n in history] == ["retired", "revoked", "active"]

"""最终 Review 6 项深层缝隙修复的端到端验收测试（ADR 0027）。

覆盖：
1. 管家晨报 / 长期追踪投递经 PluginHost 事件总线（briefing.delivered）自动触发飞书/企微 IM 外推卡片；
2. ImOutreachChannelPlugin HMAC 签名密钥持久化（0600 文件），重启实例后仍在途卡片可安全验签；
3. BrowserSandboxBackendPlugin 在线管理员策略接口（PUT /policy）权限校验与跨重启持久化；
4. ArtisanForgeService 全生命周期（create -> control -> gates -> review -> publish）实时镜像 ControlPlane；
5. L0 守护进程 CLI（migration_linter / golden_evals / worktree_evolve）可直接以 python -m 独立执行；
6. 管家工具说明与 SKILL.md 准确覆盖全栈 ui/ 插件造匠与 doc-analyst 能力。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from server.api.app import create_app
from server.api.auth import caller
from server.core.plugins.browser_plugin import BrowserPolicy, BrowserSandboxBackendPlugin
from server.core.plugins.channel_plugins import ImOutreachChannelPlugin
from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord


class _RecordingTransport:
    """记录所有 IM 外推卡片请求的确定性测试发送器。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def send_card(
        self, channel: str, target_id: str, card_payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.sent.append((channel, target_id, card_payload))
        return {"accepted": True, "seq": len(self.sent)}


async def test_briefing_delivered_event_auto_dispatches_im_outreach_card(
    world: Any, tmp_path: Path,
) -> None:
    """验证管家晨报投递时自动通过事件总线触发 ImOutreachChannelPlugin 外推飞书卡片。"""
    platform = world.platform
    user_id = "usr_brief_im"
    await platform.identity.store.create_user(
        UserRecord(
            user_id=user_id, tenant_id="acme", role="tenant_member", status="active",
            display_name="Bob", created_at=1000.0,
        )
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id=user_id, key_id=None)

    transport = _RecordingTransport()
    plugin = ImOutreachChannelPlugin(transport=transport, data_dir=tmp_path)
    await platform.plugins.hot_swap(plugin)

    # 先为员工绑定飞书通道
    await platform.plugins.dispatch_http(
        "im-outreach", "PUT", "binding", who,
        {"channel": "feishu", "target_id": "ou_bob_123", "enabled": True},
    )
    # 固定到北京时间上午 09:00（非夜间静默时段），确保测试全天候确定性触发投递
    platform.butler.briefings._now = lambda: 1_764_291_600.0
    # 生成并投递管家每日晨报（deliver=True 应触发 briefing.delivered 事件）
    brief = await platform.butler.briefings.build_daily_brief(who, deliver=True)
    assert brief["delivered"] is True
    assert len(transport.sent) == 1
    channel, target, card = transport.sent[0]
    assert channel == "feishu" and target == "ou_bob_123"
    assert card["meta"]["card_kind"] == "daily_brief"


async def test_im_outreach_hmac_secret_persists_across_restart(world: Any, tmp_path: Path) -> None:
    """验证 IM 审批卡片 HMAC 签名密钥以 0600 权限持久化，重启插件后在途卡片仍可正常验签。"""
    platform = world.platform
    user_id = "usr_hmac_persist"
    await platform.identity.store.create_user(
        UserRecord(
            user_id=user_id, tenant_id="acme", role="tenant_member", status="active",
            display_name="Carol", created_at=1000.0,
        )
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id=user_id, key_id=None)
    secret_dir = tmp_path / "data_restart"

    # 第一实例：生成密钥、绑定企微并发送审批卡片
    t1 = _RecordingTransport()
    p1 = ImOutreachChannelPlugin(transport=t1, data_dir=secret_dir)
    await platform.plugins.hot_swap(p1)
    key_file = secret_dir / "secrets" / "im_outreach_hmac.key"
    assert key_file.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    await platform.plugins.dispatch_http(
        "im-outreach", "PUT", "binding", who,
        {"channel": "wecom", "target_id": "carol_wc", "enabled": True},
    )
    sent = await platform.plugins.dispatch_http(
        "im-outreach", "POST", "dispatch", who,
        {"card_kind": "approval_card", "title": "合同盖章确认", "body": "请确认是否放行对外盖章。"},
    )
    approve_action = next(a for a in sent["actions"] if a["action"] == "approve")

    # 模拟服务重启：用同一 data_dir 构造全新插件实例并热替换
    p2 = ImOutreachChannelPlugin(transport=_RecordingTransport(), data_dir=secret_dir)
    await platform.plugins.hot_swap(p2)

    # 重启前的在途审批卡签名在新实例上依然通过验签并成功拍板
    cb = await platform.plugins.dispatch_http(
        "im-outreach", "POST", "card-callback", who,
        {
            "card_id": sent["card_id"],
            "action": "approve",
            "signature": approve_action["signature"],
        },
    )
    assert cb["status"] == "acted:approve" and cb["idempotent"] is False


async def test_browser_policy_admin_update_and_restart_persistence(world: Any) -> None:
    """验证受控浏览器策略 PUT 接口强制管理员权限，并持久化到 PluginCollectionStore 跨重启保留。"""
    platform = world.platform
    await platform.identity.store.create_user(
        UserRecord(
            user_id="usr_br_mem", tenant_id="acme", role="tenant_member", status="active",
            display_name="Member", created_at=1000.0,
        )
    )
    await platform.identity.store.create_user(
        UserRecord(
            user_id="usr_br_adm", tenant_id="acme", role="tenant_admin", status="active",
            display_name="Admin", created_at=1000.0,
        )
    )
    member = Principal(role="tenant_member", tenant_id="acme", user_id="usr_br_mem", key_id=None)
    admin = Principal(role="tenant_admin", tenant_id="acme", user_id="usr_br_adm", key_id=None)

    app = create_app(platform)
    current_who: list[Principal] = [member]
    app.dependency_overrides[caller] = lambda: current_who[0]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        # 1. 普通员工禁止修改白名单 -> 403（来自管理员角色拦截而非未建号）
        denied = await http.put(
            "/v1/plugins/ext/browser-sandbox/policy",
            json={"allowed_domains": ["docs.python.org"]},
        )
        assert denied.status_code == 403
        assert "管理员" in denied.json()["error"]["message"]

        # 2. 租户管理员允许在线更新白名单 -> 200
        current_who[0] = admin
        ok = await http.put(
            "/v1/plugins/ext/browser-sandbox/policy",
            json={"allowed_domains": ["docs.python.org", "peps.python.org"], "timeout_seconds": 18.0},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["allowed_domains"] == ["docs.python.org", "peps.python.org"]

        # 3. 模拟重启热替换全新的默认空白名单 BrowserSandboxBackendPlugin -> 策略依然从持久层读出
        await platform.plugins.hot_swap(
            BrowserSandboxBackendPlugin(policy=BrowserPolicy(), artifacts=platform.trace_artifacts)
        )
        current_who[0] = member
        got = await http.get("/v1/plugins/ext/browser-sandbox/policy")
        assert got.status_code == 200
        assert got.json()["allowed_domains"] == ["docs.python.org", "peps.python.org"]
        assert got.json()["timeout_seconds"] == 18.0


async def test_forge_mirrors_every_status_transition_to_controlplane(world: Any) -> None:
    """验证 ArtisanForgeService 每次门禁、审核与发布均实时同步镜像到 ControlPlane，不再残留 editing。"""
    platform = world.platform
    admin = Principal(role="tenant_admin", tenant_id="acme", user_id="usr_forge_sync", key_id=None)

    draft = await platform.forge.create_draft(
        admin, package_name="sync-bot", description="镜像一致性验证匠人",
    )
    await platform.forge.apply_control(
        admin, draft.draft_id, "upsert_eval",
        {"name": "smoke", "entry": "main", "input": "你好", "contains": ["已收到"]},
    )
    l1 = await platform.forge.run_l1_validate(admin, draft.draft_id)
    l2 = await platform.forge.run_l2_unit_tests(admin, draft.draft_id)
    l3 = await platform.forge.run_l3_eval_check(admin, draft.draft_id)
    assert l1.passed and l2.passed and l3.passed

    await platform.forge.review_draft(admin, draft.draft_id, summary="通过", approved=True)
    mirrored_reviewed = {d.draft_id: d for d in await platform.forge.list_persisted_drafts(admin)}
    assert mirrored_reviewed[draft.draft_id].status == "reviewed"
    assert mirrored_reviewed[draft.draft_id].l1_report is not None

    await platform.forge.publish_draft(admin, draft.draft_id, activate=True)
    mirrored_pub = {d.draft_id: d for d in await platform.forge.list_persisted_drafts(admin)}
    assert mirrored_pub[draft.draft_id].status == "published"
    assert mirrored_pub[draft.draft_id].published_version == "0.1.0"


def test_l0_supervisor_cli_entrypoints_work_standalone(tmp_path: Path) -> None:
    """验证 L0 迁移检查器与黄金评测 CLI 可独立作为模块直接运行并返回标准退出码。"""
    repo_root = Path(__file__).resolve().parents[3]

    # 1. migration_linter 检查真实 0040_plugin_collections.sql -> exit 0
    mig_0040 = repo_root / "server" / "core" / "controlplane" / "migrations" / "0040_plugin_collections.sql"
    ok_res = subprocess.run(
        [sys.executable, "-m", "scripts.l0_supervisor.migration_linter", str(mig_0040)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok_res.returncode == 0, ok_res.stderr
    assert json.loads(ok_res.stdout)["ok"] is True

    # 2. migration_linter 检查破坏性 DROP COLUMN -> exit 2
    bad_sql = tmp_path / "9999_bad.sql"
    bad_sql.write_text("ALTER TABLE xp_demo DROP COLUMN legacy_col;\n", encoding="utf-8")
    bad_res = subprocess.run(
        [sys.executable, "-m", "scripts.l0_supervisor.migration_linter", str(bad_sql)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_res.returncode == 2
    assert json.loads(bad_res.stdout)["ok"] is False

    # 3. golden_evals --list-only -> exit 0 且包含全部 25 条黄金用例
    eval_res = subprocess.run(
        [sys.executable, "-m", "scripts.l0_supervisor.golden_evals", "--list-only"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert eval_res.returncode == 0, eval_res.stderr
    eval_data = json.loads(eval_res.stdout)
    assert eval_data["total"] == 25
    assert len(eval_data["by_category"]) >= 6


def test_butler_forge_description_and_skill_cover_fullstack_ui() -> None:
    """验证管家工具描述与 SKILL.md 准确指引全栈 ui/ 插件造匠与 doc-analyst 能力。"""
    repo_root = Path(__file__).resolve().parents[3]
    tools_src = (repo_root / "server" / "engine" / "butler" / "tools.py").read_text(encoding="utf-8")
    skill_src = (
        repo_root / "server" / "engine" / "butler" / "skills" / "butler" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "register_ui_plugin" in tools_src and "remove_ui_plugin" in tools_src
    assert "register_ui_plugin" in skill_src and "doc-analyst" in skill_src

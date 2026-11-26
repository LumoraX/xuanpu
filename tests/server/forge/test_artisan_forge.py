"""管家造匠（Artisan Forge）、三层修改边界与 L1/L2/L3 不可变发布节点测试（ADR 0025）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from server.api.app import create_app
from server.api.auth import caller
from server.core.errors import InvalidRequestError
from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord
from server.engine.forge.content_tools import ForgeContentEditor
from server.engine.forge.control_tools import (
    forge_add_entry,
    forge_bump_version,
    forge_remove_entry,
    forge_set_connector_tools,
    forge_set_policy,
    forge_upsert_eval,
)
from server.engine.forge.models import FORGE_VERSION, ReleaseNode
from server.engine.forge.service import verify_bidirectional_compatibility
from spec import MANIFEST_FILENAME, PackageManifest, dump_manifest

REPO_ROOT = Path(__file__).resolve().parents[3]


async def test_l_content_hook_blocks_raw_manifest_edit_and_checks_python_syntax(
    tmp_path: Path,
) -> None:
    """L-Content PreToolUseHook 硬拒绝裸改 xuanpu.yaml 与路径穿越；PostToolUseHook 自动查语法。"""
    draft = tmp_path / "draft"
    (draft / "skills" / "main").mkdir(parents=True)
    (draft / MANIFEST_FILENAME).write_text("apiVersion: xuanpu/v1\n", encoding="utf-8")
    (draft / "skills" / "main" / "SKILL.md").write_text(
        "---\nname: main\ndescription: demo\nversion: 1.0.0\nentry: true\n---\n# Demo\n",
        encoding="utf-8",
    )
    editor = ForgeContentEditor(draft)

    # 1) 严禁 LLM 裸改 xuanpu.yaml
    denied = await editor.apply_patches([{
        "path": "xuanpu.yaml",
        "old_text": "apiVersion: xuanpu/v1",
        "new_text": "apiVersion: hacked/v9",
    }])
    assert denied.ok is False
    assert "L-Control" in denied.message
    assert (draft / MANIFEST_FILENAME).read_text(encoding="utf-8") == "apiVersion: xuanpu/v1\n"

    # 2) 严禁路径穿越
    escaped = await editor.apply_patches([{"path": "../escape.txt", "create": True, "new_text": "x"}])
    assert escaped.ok is False

    # 3) 合法子目录写入合法 Python 脚本 → 成功并产出 Diff，无语法告警
    ok_py = await editor.apply_patches([{
        "path": "skills/main/scripts/calc.py",
        "create": True,
        "new_text": "def add(a: int, b: int) -> int:\n    return a + b\n",
    }])
    assert ok_py.ok is True
    assert "+def add" in ok_py.diffs["skills/main/scripts/calc.py"]
    assert ok_py.post_checks == ()

    # 4) 写入语法错误脚本 → PostToolUseHook 自动捕获并回报告警
    bad_py = await editor.apply_patches([{
        "path": "skills/main/scripts/broken.py",
        "create": True,
        "new_text": "def bad(:\n    pass\n",
    }])
    assert bad_py.ok is True
    assert any("语法错误" in w for w in bad_py.post_checks)


def test_l_control_typed_mutations_and_invariants(tmp_path: Path) -> None:
    """L-Control 强类型代码工具保证合法清单、拒绝移除最后入口、拒绝通配连接器工具。"""
    draft = tmp_path / "pkg"
    draft.mkdir()
    manifest = PackageManifest.model_validate({
        "apiVersion": "xuanpu/v1",
        "kind": "Package",
        "metadata": {"name": "demo-bot", "version": "1.0.0", "description": "演示包"},
        "spec": {
            "skills": {"entries": ["main"]},
            "connectors": [{
                "name": "crm",
                "command": ["echo", "mcp"],
                "tools": [{"name": "get_user", "effect": "read"}],
            }],
        },
    })
    (draft / MANIFEST_FILENAME).write_text(dump_manifest(manifest), encoding="utf-8")

    # 禁止移除最后一个入口
    with pytest.raises(InvalidRequestError, match="最后一个入口"):
        forge_remove_entry(draft, skill_id="main")

    # 扩充新入口 + 修改策略 + 登记评测 + 递增版本
    added = forge_add_entry(draft, skill_id="helper", display_name="辅助入口")
    assert "helper" in added.manifest.spec.skills.entries
    assert (draft / "skills" / "helper" / "SKILL.md").is_file()

    pol = forge_set_policy(
        draft, approval="strict", max_session_tokens=100_000, sandbox_shell=True, model_tier="deep"
    )
    assert pol.manifest.spec.policy.approval == "strict"
    assert pol.manifest.spec.model.tier == "deep"

    # 连接器工具白名单禁止空或通配符
    with pytest.raises(InvalidRequestError, match="不能为空"):
        forge_set_connector_tools(draft, connector_name="crm", tools=[])
    with pytest.raises(InvalidRequestError, match="通配符"):
        forge_set_connector_tools(draft, connector_name="crm", tools=[{"name": "*"}])

    ev = forge_upsert_eval(
        draft, name="smoke", entry="helper", input_text="你好", contains=("您好",), max_tokens=2000
    )
    assert len(ev.manifest.spec.evals) == 1

    bumped = forge_bump_version(draft, bump="minor")
    assert bumped.manifest.metadata.version == "1.1.0"

    removed = forge_remove_entry(draft, skill_id="helper")
    assert removed.manifest.spec.skills.entries == ("main",)


def test_l2_bidirectional_compatibility_with_existing_support_desk() -> None:
    """L2 构造器必须无损兼容存量业务包 examples/support-desk。"""
    assert verify_bidirectional_compatibility(REPO_ROOT / "examples" / "support-desk") is True


async def test_full_forge_lifecycle_publish_rollback_and_clean_unmounting(world: Any) -> None:
    """端到端：造匠 → L1/L2/L3 门禁 → 管家审核 → 发版 v0.1.0 → 迭代 v0.1.1 → 回退 R3→v0.1.0 → 熔断与干净卸载。"""
    platform = world.platform
    await platform.identity.store.create_user(UserRecord(
        tenant_id="acme", user_id="usr_alice", display_name="Alice", created_at=1.0
    ))
    alice = Principal(role="tenant_admin", tenant_id="acme", key_id=None, user_id="usr_alice")
    unmounted: list[tuple[str, str]] = []

    async def on_unmount(node: ReleaseNode, state: str) -> None:
        unmounted.append((node.version, state))

    platform.releases.unmount_hooks.append(on_unmount)

    app = create_app(platform)
    app.dependency_overrides[caller] = lambda: alice

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        # 1. 创建草稿
        d1 = (await http.post("/v1/forge/drafts", json={
            "package_name": "calc-artisan",
            "description": "公式核验匠人",
            "entry": "calc",
            "display_name": "公式匠人",
        })).json()
        draft1_id = d1["draft_id"]
        assert d1["built_by"]["forge_version"] == FORGE_VERSION

        # 2. L-Content 写入业务脚本与单测（禁止裸改 xuanpu.yaml）
        bad = (await http.post(f"/v1/forge/drafts/{draft1_id}/content", json={
            "patches": [{"path": "xuanpu.yaml", "create": True, "new_text": "bad"}]
        })).json()
        assert bad["ok"] is False

        ok_patch = (await http.post(f"/v1/forge/drafts/{draft1_id}/content", json={
            "patches": [
                {
                    "path": "skills/calc/scripts/math_tool.py",
                    "create": True,
                    "new_text": "def double(x: int) -> int:\n    return x * 2\n",
                },
                {
                    "path": "tests/test_math.py",
                    "create": True,
                    "new_text": (
                        "import sys; sys.path.insert(0, 'skills/calc/scripts')\n"
                        "from math_tool import double\n"
                        "assert double(21) == 42\n"
                    ),
                },
            ]
        })).json()
        assert ok_patch["ok"] is True

        # 3. L-Control 登记评测用例
        ctrl = await http.post(f"/v1/forge/drafts/{draft1_id}/control", json={
            "action": "upsert_eval",
            "params": {"name": "basic", "entry": "calc", "input": "21*2", "contains": ["42"]},
        })
        assert ctrl.status_code == 200

        # 未跑门禁前禁止批准发布
        premature = await http.post(f"/v1/forge/drafts/{draft1_id}/review", json={
            "summary": "试图跳过门禁", "approved": True,
        })
        assert premature.status_code == 409

        # 4. 跑完 L1 + L2 + L3 门禁 → 管家审核 → 发布 v0.1.0（Generation 1）
        gates = (await http.post(f"/v1/forge/drafts/{draft1_id}/gates")).json()
        assert gates["passed"] is True
        rev1 = await http.post(f"/v1/forge/drafts/{draft1_id}/review", json={
            "summary": "L1/L2/L3 全绿，权限边界清晰，准予发布", "approved": True,
        })
        assert rev1.status_code == 200

        pub1_resp = await http.post(f"/v1/forge/drafts/{draft1_id}/publish")
        assert pub1_resp.status_code == 200, pub1_resp.text
        pub1 = pub1_resp.json()
        rel1_id = pub1["release"]["release_id"]
        assert pub1["release"]["generation"] == 1
        assert pub1["release"]["version"] == "0.1.0"
        assert await platform.packages.active_version("acme", "calc-artisan") == "0.1.0"

        # 5. 从 v0.1.0 检出迭代为 v0.1.1（Generation 2），验证旧版自动 retired + Clean Unmounting
        d2 = (await http.post("/v1/forge/drafts/checkout", json={
            "package_name": "calc-artisan", "version": "0.1.0",
        })).json()
        draft2_id = d2["draft_id"]
        await http.post(f"/v1/forge/drafts/{draft2_id}/control", json={
            "action": "bump_version", "params": {"bump": "patch"},
        })
        assert (await http.post(f"/v1/forge/drafts/{draft2_id}/gates")).json()["passed"] is True
        await http.post(f"/v1/forge/drafts/{draft2_id}/review", json={
            "summary": "小版本升级审核通过", "approved": True,
        })
        pub2 = (await http.post(f"/v1/forge/drafts/{draft2_id}/publish")).json()
        assert pub2["release"]["generation"] == 2
        assert pub2["release"]["version"] == "0.1.1"
        assert ("0.1.0", "retired") in unmounted

        # 6. 秒级回退到 Generation 1（追加 Generation 3 指向 0.1.0，历史完整保留）
        rb = (await http.post("/v1/releases/artisan/calc-artisan/rollback", json={
            "to_release_id": rel1_id, "mode": "revoked",
        })).json()
        assert rb["generation"] == 3
        assert rb["version"] == "0.1.0"
        assert rb["rollback_of"] == pub2["release"]["release_id"]
        assert await platform.packages.active_version("acme", "calc-artisan") == "0.1.0"
        assert ("0.1.1", "revoked") in unmounted

        hist = (await http.get("/v1/releases/artisan/calc-artisan")).json()["items"]
        assert [h["generation"] for h in hist] == [3, 2, 1]

        # 7. 独立检验 L2 构造器与 L3 内核发布节点（互不干扰 L1）
        l2_r1 = await platform.releases.promote(
            "acme", layer="forge", target_name="artisan-forge",
            version="2026.11.27.1", artifact_digest="sha256:" + "a" * 64, actor="admin",
        )
        l2_r2 = await platform.releases.promote(
            "acme", layer="forge", target_name="artisan-forge",
            version="2026.11.27.2", artifact_digest="sha256:" + "b" * 64, actor="admin",
        )
        l2_rb = await platform.releases.rollback(
            "acme", layer="forge", target_name="artisan-forge",
            to_release_id=l2_r1.release_id, actor="admin",
        )
        assert l2_r2.generation == 2 and l2_rb.generation == 3 and l2_rb.version == "2026.11.27.1"
        # L1 匠人版本完全不受 L2 构造器变更与回退影响
        assert await platform.packages.active_version("acme", "calc-artisan") == "0.1.0"

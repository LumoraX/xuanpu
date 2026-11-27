"""微内核前后端插件基础设施测试（ADR 0027）：BackendPlugin 生命周期/热替换/干净卸载、零 DDL 集合、全栈 ui/ 造匠与 Bridge RPC 权限。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from server.api.app import create_app
from server.api.auth import caller
from server.core.errors import ConflictError, NotFoundError
from server.core.plugins.models import BackendPluginManifest, PluginHealth
from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord
from spec import scan_ui_asset_security

if TYPE_CHECKING:
    from server.core.plugins.host import PluginContext


async def _employee_client(world: Any) -> tuple[httpx.AsyncClient, Principal]:
    """在 world 租户 acme 下注册一名活跃员工并构造绑定其身份的 AsyncClient。"""
    platform = world.platform
    user_id = "usr_plugin_alice"
    await platform.identity.store.create_user(
        UserRecord(
            user_id=user_id,
            tenant_id="acme",
            role="tenant_member",
            status="active",
            display_name="Alice",
            created_at=1000.0,
        )
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id=user_id, key_id=None)
    app = create_app(platform)
    app.dependency_overrides[caller] = lambda: who
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, who


class _SampleWorkerPlugin:
    """用于验证挂载、事件订阅、后台任务、热替换与干净卸载的测试插件。"""

    def __init__(self, version: str, *, fail_mount: bool = False) -> None:
        self.manifest = BackendPluginManifest(
            plugin_id="sample-ext",
            version=version,
            display_name="示例扩展插件",
            description="测试微内核插件生命周期",
            trust_level="in_process_verified",
            permissions=("collection.read",),
        )
        self._fail_mount = fail_mount
        self.events_seen: list[dict[str, Any]] = []
        self.cleaned_up = False
        self.bg_cancelled = False

    async def mount(self, ctx: PluginContext) -> None:
        if self._fail_mount:
            raise RuntimeError("模拟新版插件启动失败")

        async def _ping(_who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {"version": self.manifest.version, "echo": payload.get("msg", "")}

        async def _on_evt(_name: str, payload: dict[str, Any]) -> None:
            self.events_seen.append(payload)

        async def _tool(_who: Principal, args: dict[str, Any]) -> dict[str, Any]:
            return {"tool_version": self.manifest.version, "x": args.get("x")}

        async def _bg() -> None:
            try:
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.bg_cancelled = True
                raise

        async def _cleanup() -> None:
            self.cleaned_up = True

        ctx.add_route("GET", "ping", _ping)
        ctx.add_route("POST", "ping", _ping)
        ctx.on_event("sample.fired", _on_evt)
        ctx.add_tool("echo", "回显测试", {"x": {"type": "string"}}, _tool)
        ctx.spawn_task(_bg(), name="heartbeat")
        ctx.add_cleanup(_cleanup)

    async def health(self) -> PluginHealth:
        return PluginHealth(ok=True, detail=f"v{self.manifest.version}", checked_at=datetime.now(UTC))

    async def unmount(self, *, drain_timeout: float = 15.0) -> None:
        _ = drain_timeout


async def test_backend_plugin_lifecycle_hot_swap_and_clean_unmount(
    world: Any,
) -> None:
    """验证后端插件动态路由、热替换（失败自动保留旧版）与 Clean Unmounting（零残留卸载）。"""
    platform = world.platform
    http, who = await _employee_client(world)
    async with http:
        # 1. 内置 L2 custom-records 插件已随平台启动自动挂载并支持零 DDL CRUD 与乐观锁
        listed = await http.get("/v1/plugins")
        assert listed.status_code == 200, listed.text
        by_id = {p["plugin_id"]: p for p in listed.json()["items"]}
        assert "custom-records" in by_id
        assert by_id["custom-records"]["state"] == "mounted"
        assert by_id["custom-records"]["health"]["ok"] is True

        created = await http.post(
            "/v1/plugins/ext/custom-records/items",
            json={"data": {"title": "合同核对清单", "score": 95}},
        )
        assert created.status_code == 200, created.text
        rec = created.json()
        assert rec["version"] == 1 and rec["data"]["title"] == "合同核对清单"

        # 乐观锁版本冲突必须返回 409
        conflict = await http.post(
            "/v1/plugins/ext/custom-records/items",
            json={"record_id": rec["record_id"], "expected_version": 99, "data": {"title": "冲突"}},
        )
        assert conflict.status_code == 409

        # 2. 动态挂载 v1.0.0 插件 -> 路由、事件、工具全部生效
        v1 = _SampleWorkerPlugin("1.0.0")
        platform.plugins.register(v1)
        await platform.plugins.mount("sample-ext")

        r1 = await http.post("/v1/plugins/ext/sample-ext/ping", json={"msg": "hi"})
        assert r1.status_code == 200 and r1.json() == {"version": "1.0.0", "echo": "hi"}
        assert await platform.plugins.emit_event("sample.fired", {"n": 1}) == 1
        assert v1.events_seen == [{"n": 1}]
        tool_res = await platform.plugins.call_tool("plugin__sample-ext__echo", who, {"x": "ok"})
        assert tool_res == {"tool_version": "1.0.0", "x": "ok"}

        # 3. 热替换为 v2.0.0 -> 新版本接管且 v1 后台任务与清理钩子被彻底回收
        v2 = _SampleWorkerPlugin("2.0.0")
        await platform.plugins.hot_swap(v2)
        assert v1.cleaned_up is True and v1.bg_cancelled is True
        r2 = await http.post("/v1/plugins/ext/sample-ext/ping", json={"msg": "hi2"})
        assert r2.status_code == 200 and r2.json() == {"version": "2.0.0", "echo": "hi2"}

        # 4. 热替换为故障版 v3.0.0 -> 抛错且自动保持 v2.0.0 继续在线服务
        v3_bad = _SampleWorkerPlugin("3.0.0", fail_mount=True)
        with pytest.raises(RuntimeError, match="启动失败"):
            await platform.plugins.hot_swap(v3_bad)
        r2_still = await http.get("/v1/plugins/ext/sample-ext/ping")
        assert r2_still.status_code == 200 and r2_still.json()["version"] == "2.0.0"

        # 5. Clean Unmounting：卸载后路由立即 404、事件不再触发、工具消失、后台任务回收
        await platform.plugins.unmount("sample-ext")
        assert v2.cleaned_up is True and v2.bg_cancelled is True
        r_gone = await http.get("/v1/plugins/ext/sample-ext/ping")
        assert r_gone.status_code == 404
        assert await platform.plugins.emit_event("sample.fired", {"n": 2}) == 0
        with pytest.raises(NotFoundError):
            await platform.plugins.call_tool("plugin__sample-ext__echo", who, {"x": "no"})

        # 重复注册同名插件报 ConflictError
        with pytest.raises(ConflictError):
            platform.plugins.register(_SampleWorkerPlugin("4.0.0"))


def test_ui_asset_static_security_scanner_blocks_dangerous_patterns() -> None:
    """前端插件静态安全扫描拦截 Cookie 窃取、越界访问、eval、外链脚本与直连网络。"""
    bad = (
        "<script src='https://evil.example/x.js'></script>"
        "<script>const c = document.cookie; eval('1'); fetch('/v1/me'); window.top.x = 1;</script>"
    )
    problems = scan_ui_asset_security("ui/bad/index.html", bad)
    assert len(problems) >= 5
    assert any("document.cookie" in p for p in problems)
    assert any("CDN" in p for p in problems)
    assert any("postMessage Bridge" in p for p in problems)

    good = "<div id='app'>hello</div><script>window.parent.postMessage({type:'xuanpu:ready'},'*');</script>"
    assert scan_ui_asset_security("ui/good/index.html", good) == []


async def test_full_stack_artisan_ui_forge_publish_asset_serving_and_bridge_rpc(
    world: Any,
) -> None:
    """全栈匠人造匠（Python + ui/ 同包版本化）→ L1/L2/L3 门禁发版 → /v1/plugins/ui 发现与严格 CSP 托管 → Bridge RPC 权限裁决。"""
    platform = world.platform
    admin = world.admin
    http, _ = await _employee_client(world)
    async with http:
        # 1. 通过 Forge L-Control 新建全栈匠人并登记同包 ui/ 插件（只授 collection.read/write 与 workspace.propose，不授 artifact.read）
        rec = await platform.forge.create_draft(
            admin, package_name="invoice-bot", description="财务审单全栈匠人", entry="main",
        )
        await platform.forge.apply_control(
            admin,
            rec.draft_id,
            "register_ui_plugin",
            {
                "plugin_id": "invoice-board",
                "title": "审单对照台",
                "slot": "aux-tab",
                "mode": "sandbox_iframe",
                "bridge_permissions": ["collection.read", "collection.write", "workspace.propose"],
            },
        )
        await platform.forge.apply_control(
            admin,
            rec.draft_id,
            "upsert_eval",
            {"name": "smoke", "entry": "main", "input": "核对发票", "contains": ["完成"]},
        )

        # 2. L-Content Hook 护栏：往 ui/ 注入越权脚本时 PostToolUseHook 自动告警，且 L1 校验直接拒绝
        bad_patch = await platform.forge.apply_content_patches(
            admin,
            rec.draft_id,
            [{"path": "ui/invoice-board/evil.js", "create": True, "new_text": "const x = document.cookie;"}],
        )
        assert bad_patch.ok
        assert any("document.cookie" in w for w in bad_patch.post_checks)
        l1_bad = await platform.forge.run_l1_validate(admin, rec.draft_id)
        assert not l1_bad.passed

        # 删除违规文件后，全套 L1 + L2 + L3 通过并审核发版
        await platform.forge.apply_content_patches(
            admin,
            rec.draft_id,
            [{"path": "ui/invoice-board/evil.js", "delete": True}],
        )
        l1 = await platform.forge.run_l1_validate(admin, rec.draft_id)
        l2 = await platform.forge.run_l2_unit_tests(admin, rec.draft_id)
        l3 = await platform.forge.run_l3_eval_check(admin, rec.draft_id)
        assert l1.passed and l2.passed and l3.passed, (l1.errors, l2.errors, l3.errors)

        await platform.forge.review_draft(admin, rec.draft_id, summary="全栈审核通过", approved=True)
        draft, node = await platform.forge.publish_draft(admin, rec.draft_id)
        assert draft.status == "published" and node.generation == 1

        # 3. 前端发现接口 /v1/plugins/ui 自动返回 L2 内置插件 + L1 invoice-bot 同包插件
        ui_list = await http.get("/v1/plugins/ui")
        assert ui_list.status_code == 200, ui_list.text
        ui_items = {item["plugin_id"]: item for item in ui_list.json()["items"]}
        assert "builtin:mermaid-svg" in ui_items
        assert "artisan:invoice-bot:invoice-board" in ui_items
        entry_url = ui_items["artisan:invoice-bot:invoice-board"]["entry_url"]
        assert entry_url == "/v1/plugins/ui/invoice-bot/0.1.0/invoice-board/index.html"

        # 4. 静态资源托管带严格沙盒 CSP（禁用网络与同源越权）并阻断路径穿越
        asset = await http.get(entry_url)
        assert asset.status_code == 200
        assert "审单对照台" in asset.text
        csp = asset.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp and "connect-src 'none'" in csp
        assert asset.headers.get("x-content-type-options") == "nosniff"

        traversal = await http.get("/v1/plugins/ui/invoice-bot/0.1.0/%2e%2e/xuanpu.yaml")
        assert traversal.status_code in (400, 404)

        # 5. postMessage Bridge RPC 网关：放行已声明权限，硬拒未声明权限
        write_rpc = await http.post(
            "/v1/plugins/bridge/rpc",
            json={
                "plugin_id": "artisan:invoice-bot:invoice-board",
                "package": "invoice-bot",
                "version": "0.1.0",
                "method": "collection.write",
                "params": {"collection": "drafts", "data": {"invoice_no": "INV-001", "amount": 8800}},
            },
        )
        assert write_rpc.status_code == 200, write_rpc.text
        assert write_rpc.json()["ok"] is True

        read_rpc = await http.post(
            "/v1/plugins/bridge/rpc",
            json={
                "plugin_id": "artisan:invoice-bot:invoice-board",
                "method": "collection.read",
                "params": {"collection": "drafts"},
            },
        )
        assert read_rpc.status_code == 200
        assert read_rpc.json()["result"]["items"][0]["data"]["invoice_no"] == "INV-001"

        propose_rpc = await http.post(
            "/v1/plugins/bridge/rpc",
            json={
                "plugin_id": "artisan:invoice-bot:invoice-board",
                "method": "workspace.propose",
                "params": {"summary": "核对通过，建议提交财务审批"},
            },
        )
        assert propose_rpc.status_code == 200
        assert propose_rpc.json()["result"]["state"] == "proposed"

        # 未声明 artifact.read 必须返回 403
        denied_rpc = await http.post(
            "/v1/plugins/bridge/rpc",
            json={
                "plugin_id": "artisan:invoice-bot:invoice-board",
                "method": "artifact.read",
                "params": {"artifact_id": "art_any"},
            },
        )
        assert denied_rpc.status_code == 403

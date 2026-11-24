"""R5-E 极简入口的真实浏览器验收：本机 loopback、内存平台、模型屏蔽、两名测试员工。

用法（需要本机 Chrome；不连外部服务，不用任何真实身份或模型）::

    .venv/bin/python scripts/acceptance/r5_minimal_entry_browser.py \
        --chrome "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --out <目录>

检查项：首屏四样；模型屏蔽时概览、提示、「更多」可用；两员工互不可见；375px 宽内嵌框里
无横向溢出（经 DevTools 协议模拟移动设备，量 ``scrollWidth`` 并截图）；
默认页脚本数与体积。结果写进 ``<目录>/evidence.json``，截图为 ``<目录>/alice-375.png``。
对话内逐模块查看与确认需要真实点击，本脚本不覆盖，由 Node 行为测试覆盖。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import re
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PARTS = ('id="overview"', 'id="transcript"', 'id="tips"', 'id="composer"')
ALICE_ONLY = "验收-Alice的提醒"


async def assemble(directory: Path, origin: str, resources: AsyncExitStack) -> tuple[Any, dict[str, str]]:
    """内存平台：acme 开启追踪、管家与极简入口；模型一律拒绝；两名员工与登录 cookie。"""
    from scripts.acceptance.finance_browser import ModelBlocker
    from server.engine.butler.config import ButlerConfig
    from server.core.controlplane import InMemoryControlPlane
    from server.domains.dashboard.config import MinimalEntryConfig
    from server.domains.identity import InMemoryIdentityStore
    from server.domains.identity.config import IdentityConfig
    from server.domains.identity.service import IdentitySettings
    from server.core.objectstore import LocalObjectStore
    from server.platform import ROOT_PRINCIPAL, Platform
    from server.engine.runtime import LocalSandboxProvider, RuntimeDefaults
    from server.core.secrets import EncryptedVault, generate_master_key
    from server.core.tenancy import generate_root_key
    from server.engine.tracing.config import ExecutionTraceConfig

    store, blocker = InMemoryControlPlane(), ModelBlocker()
    _, root_hash = generate_root_key()
    (directory / "data").mkdir()
    platform = Platform(
        store=store, identity_store=InMemoryIdentityStore(store),
        identity_settings=IdentitySettings.from_config(origin, IdentityConfig(cookie_secure=False)),
        vault=EncryptedVault(store, keys={"k1": generate_master_key()}, primary="k1"),
        models=blocker, sandboxes=LocalSandboxProvider(), data_dir=directory / "data",
        root_key_hash=root_hash, instance_id="inst-r5e-browser",
        objects=LocalObjectStore(directory / "objects"),
        defaults=RuntimeDefaults(execution_trace=ExecutionTraceConfig(enabled=True, tenants=("acme",))),
        butler=ButlerConfig(enabled=True, tenants=("acme",)),
        minimal_entry=MinimalEntryConfig(enabled=True, tenants=("acme",)),
    )
    resources.push_async_callback(platform.close)
    await platform.start()
    await platform.create_tenant(ROOT_PRINCIPAL, tenant_id="acme", name="R5-E验收租户")
    identity = importlib.import_module("packages.xuanpu-server.tests.identity.conftest")
    world = SimpleNamespace(platform=platform, models=blocker)
    ident = identity.IdentityWorld(world=world, http=None, feishu=None)
    cookies: dict[str, str] = {"cookie_name": ident.cookie_name}
    for actor, name in (("alice", "验收员工甲"), ("bob", "验收员工乙")):
        person = await ident.user(name=name)
        cookies[actor] = await ident.login(person)
        if actor == "alice":
            await seed(platform, person.user_id)
    return SimpleNamespace(platform=platform, blocker=blocker), cookies


async def seed(platform: Any, user_id: str) -> None:
    """Alice：一条稍后到期的提醒与一条已送达未读的通知。"""
    from server.domains.reminders.models import ReminderInput
    from server.core.tenancy import Principal

    who = Principal(role="tenant_member", tenant_id="acme", user_id=user_id, key_id=None)
    soon = time.time() + 120
    await platform.reminders.create(who, ReminderInput(title=ALICE_ONLY, due_at=soon, timezone="UTC"), "seed-1")
    fired = await platform.reminders.create(who, ReminderInput(title="验收-已到期", due_at=soon, timezone="UTC"), "seed-2")
    await platform.reminders.store.fire(fired, soon + 1)


def add_login(app: FastAPI, cookies: dict[str, str]) -> None:
    """仅本次 loopback 应用：签发测试员工会话并进入 /console/（产品路由不变）。"""
    from fastapi.responses import RedirectResponse

    @app.get("/__acceptance__/login/{actor}")
    async def login(actor: str, classic: bool = False) -> RedirectResponse:
        """未知角色不发 cookie。``classic`` 为真时直接进「更多」（功能页），同一次浏览器会话内完成。"""
        target = "/console/index.html?classic=1" if classic else "/console/"
        response = RedirectResponse(target, status_code=303)
        if actor in ("alice", "bob"):
            response.set_cookie(cookies["cookie_name"], cookies[actor], httponly=True, samesite="lax")
        return response


def chrome(binary: str, url: str, profile: Path, *extra: str) -> str:
    """一次无头 Chrome：独立配置目录（cookie 不串），虚拟时间等异步请求跑完。"""
    result = subprocess.run(
        [binary, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         f"--user-data-dir={profile}", "--virtual-time-budget=15000", *extra, url],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("CHROME_FAILED " + result.stderr[-400:])
    return result.stdout


def page_weight() -> dict[str, Any]:
    """默认页（entry.html）加载的脚本数与体积（字节，未压缩）。"""
    from web import static_dir

    directory = static_dir()
    html = (directory / "entry.html").read_text(encoding="utf-8")
    scripts = [s.split("?")[0] for s in re.findall(r'<script src="([^"]+)"', html)]
    styles = [s.split("?")[0] for s in re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)]
    return {"scripts": len(scripts), "script_bytes": sum((directory / s).stat().st_size for s in scripts),
            "style_bytes": sum((directory / s).stat().st_size for s in styles), "files": scripts + styles}


def check_dom(dom: str, *, own: bool) -> dict[str, Any]:
    """首屏四样、概览四块、无功能页脚本、本人数据可见与否。"""
    return {
        "four_parts": all(part in dom for part in PARTS),
        "blocks": sorted(set(re.findall(r'data-key="([a-z_]+)"', dom))),
        "more_link": 'href="index.html?classic=1"' in dom,
        "tips_rendered": bool(re.search(r'id="tips"[^>]*>\s*<button', dom)),
        "alice_data_visible": ALICE_ONLY in dom,
        "classic_scripts_loaded": "app.js" in dom,
        "ok": own == (ALICE_ONLY in dom),
    }


async def run(binary: str, out: Path) -> dict[str, Any]:
    """起服务、跑浏览器、汇总证据。"""
    from scripts.acceptance.finance_browser import free_port, listen
    from web import static_dir
    from server.api.app import create_app

    out.mkdir(parents=True, exist_ok=True)
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        async with AsyncExitStack() as resources:
            env, cookies = await assemble(directory, origin, resources)
            app = create_app(env.platform, console_dir=static_dir())
            add_login(app, cookies)
            async with listen(app, port):
                evidence = await asyncio.to_thread(browse, binary, origin, directory, out)
            evidence["model_attempts"] = env.blocker.attempts
    evidence["page_weight"] = page_weight()
    (out / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return evidence


def browse(binary: str, origin: str, directory: Path, out: Path) -> dict[str, Any]:
    """Alice 与 Bob 各开一次首屏，再给 Alice 截一张 375px 图。"""
    evidence: dict[str, Any] = {}
    for actor in ("alice", "bob"):
        dom = chrome(binary, f"{origin}/__acceptance__/login/{actor}", directory / f"p-{actor}", "--dump-dom")
        evidence[actor] = check_dom(dom, own=actor == "alice")
    # 「更多」：同一登录配置目录再开功能页，模型屏蔽时仍停在旧页并显示功能入口与本人身份
    more = chrome(binary, f"{origin}/__acceptance__/login/alice?classic=true", directory / "p-more", "--dump-dom")
    evidence["more_page"] = {"stays_on_classic": 'id="tab-functions"' in more and 'id="overview"' not in more,
                             "functions_entry": 'id="functions-tab"' in more,
                             "logged_in": "验收员工甲" in more}
    evidence["narrow_375"] = narrow(binary, f"{origin}/__acceptance__/login/alice",
                                    out / "alice-375.png", directory / "p-narrow")
    evidence["screenshot"] = str(out / "alice-375.png")
    return evidence


def narrow(binary: str, url: str, shot: Path, profile: Path) -> dict[str, Any]:
    """经 DevTools 协议模拟 375px 移动设备（无头窗口不能窄于约 500px），量横向溢出并截图。"""
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("r5_minimal_entry_cdp.cjs")), binary, url, str(shot), str(profile)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("CDP_FAILED " + result.stderr[-400:])
    measure = json.loads(result.stdout.strip().splitlines()[-1])
    return {**measure, "no_horizontal_overflow": measure["scroll_width"] <= measure["viewport"]
            and measure["send_right"] <= measure["viewport"]}


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.chrome, args.out)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

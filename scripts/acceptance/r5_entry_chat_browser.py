"""R5-E 真实浏览器加真实模型验收：在极简入口的对话里逐个模块完成一次查看与（可确认的）一次确认。

平台与真实模型配置同 ``r5_real_model_harness.py``（模型只从 ``--model-env`` 的私有 dotenv 读）。
一名测试员工经浏览器登录 cookie 进入 ``/console/``；Node 驱动真实 Chrome（DevTools 协议）
在输入框里说话、等管家回复与卡片，草稿卡点「确认」、列表卡等读取完成
（``r5_entry_chat_cdp.cjs``）。之后以同一员工的 cookie 调原模块接口，核对确认过的记录确已保存。
需要前置数据或第二名员工才能确认的模块（业务跟进、执行安排、企业连接、共享、交接）只做查看；
企业连接在本环境按租户开启（不接真实企业微信）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from fastapi import FastAPI

# 每步：模块、说的话、预期（confirm=草稿卡确认；view=查看）；verify 给出确认后用哪个接口核对哪条标题
STEPS: list[dict[str, Any]] = [
    {"id": "reminder-confirm", "module": "提醒", "expect": "confirm", "text": "明天上午十点提醒我交周报",
     "verify": ["/v1/reminders?status=active", "周报"]},
    {"id": "reminder-view", "module": "提醒", "expect": "view", "text": "打开我的提醒列表"},
    {"id": "todo-confirm", "module": "待办", "expect": "confirm", "text": "建一个待办：整理季度总结，截止本周五 18:00",
     "verify": ["/v1/workspace/items?kind=todo", "季度总结"]},
    {"id": "todo-view", "module": "待办", "expect": "view", "text": "打开我的待办列表"},
    {"id": "event-confirm", "module": "日程", "expect": "confirm",
     "text": "下周三下午两点到三点安排一个项目复盘会", "verify": ["/v1/workspace/items?kind=event", "复盘"]},
    {"id": "event-view", "module": "日程", "expect": "view", "text": "打开我的日程列表"},
    {"id": "matter-confirm", "module": "事项", "expect": "confirm", "text": "新建一个事项：年度预算编制",
     "verify": ["/v1/workspace/items?kind=matter", "预算"]},
    {"id": "matter-view", "module": "事项", "expect": "view", "text": "打开我的事项列表"},
    {"id": "proactive-view", "module": "业务跟进", "expect": "view", "text": "打开我的业务跟进"},
    {"id": "definitions-view", "module": "执行安排", "expect": "view", "text": "打开我的执行安排"},
    {"id": "connections-view", "module": "企业连接", "expect": "view", "text": "打开我的企业连接"},
    {"id": "shared-view", "module": "共享事项", "expect": "view", "text": "打开共享事项"},
    {"id": "handoff-view", "module": "交接与审批", "expect": "view", "text": "打开我的交接和审批"},
]


async def login_cookie(platform: Any, user_id: str) -> tuple[str, str]:
    """给员工建一个登录会话，返回 (cookie 名, 令牌)。"""
    from xuanpu_server.identity import LoginSession
    from xuanpu_server.identity.cookies import login_cookie_name, new_login_token

    token, token_hash = new_login_token()
    now = time.time()
    await platform.identity.store.create_login_session(LoginSession(
        token_hash=token_hash, tenant_id="acme", user_id=user_id, created_at=now,
        expires_at=now + 3600, last_seen_at=now))
    return login_cookie_name(platform.identity.settings.cookie_secure), token


def add_login(app: FastAPI, name: str, token: str) -> None:
    """仅本次 loopback 应用：写入测试员工的登录 cookie 并进入 /console/（产品路由不变）。"""
    from fastapi.responses import RedirectResponse

    @app.get("/__acceptance__/login")
    async def login() -> RedirectResponse:
        """写 cookie 后跳到员工入口。"""
        response = RedirectResponse("/console/", status_code=303)
        response.set_cookie(name, token, httponly=True, samesite="lax")
        return response


async def verify(origin: str, name: str, token: str) -> dict[str, Any]:
    """以同一员工的 cookie 调原模块接口，核对每个确认步骤的记录确已保存。"""
    found: dict[str, Any] = {}
    async with httpx.AsyncClient(base_url=origin, cookies={name: token}, timeout=30.0) as http:
        for item in STEPS:
            if "verify" not in item:
                continue
            path, needle = item["verify"]
            response = await http.get(path)
            response.raise_for_status()
            records = response.json().get("items") or response.json().get("reminders") or []
            titles = [str(r.get("title") or (r.get("data") or {}).get("title") or "") for r in records]
            found[item["id"]] = any(needle in t for t in titles)
    return found


def drive(binary: str, url: str, directory: Path, out: Path) -> list[dict[str, Any]]:
    """跑 Node 驱动；步骤与结果经文件传递。"""
    steps_file, result_file = directory / "steps.json", directory / "result.json"
    steps_file.write_text(json.dumps([{k: v for k, v in s.items() if k != "verify"} for s in STEPS],
                                     ensure_ascii=False))
    result = subprocess.run(  # noqa: S603
        ["node", str(Path(__file__).with_name("r5_entry_chat_cdp.cjs")), binary, url,
         str(directory / "profile"), str(steps_file), str(result_file), str(out / "entry-chat.png")],
        capture_output=True, text=True, timeout=60 * 40, check=False)
    if result.returncode != 0:
        raise RuntimeError("CDP_FAILED " + result.stderr[-600:])
    return list(json.loads(result_file.read_text()))


def judge(steps: list[dict[str, Any]], saved: dict[str, bool]) -> dict[str, Any]:
    """每步结论：确认步须点到「确认」、看到「已保存」且接口里有记录；查看步须有回复，
    且出现该模块的卡片（「进展」卡不算）并读取完成。"""
    verdicts = {}
    for record in steps:
        replies = [n["text"] for n in record["nodes"] if n["kind"] == "assistant"]
        cards = [n for n in record["nodes"] if n["kind"] == "card"]
        module_cards = [c for c in cards if c["title"] != "进展"]
        if record["expect"] == "confirm":
            ok = bool(record.get("clicked") and record.get("saved") and saved.get(record["id"]))
        else:
            ok = bool(replies and module_cards) and "error" not in record and record.get("loaded", False)
        verdicts[record["id"]] = {"ok": ok, "cards": [c["title"] for c in cards],
                                  "reply": (replies[0] if replies else "")[:200]}
    return verdicts


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """起平台与浏览器，逐步执行并核对。"""
    from xuanpu_console import static_dir
    from xuanpu_server.api.app import create_app

    from scripts.acceptance import r5_real_model_harness as harness
    from scripts.acceptance.finance_browser import free_port, listen

    env = harness.load_model_env(args.model_env)
    args.out.mkdir(parents=True, exist_ok=True)
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        async with AsyncExitStack() as resources:
            platform, root_key = await harness.assemble(directory, env, resources, origin=origin,
                                                         connections=True)
            app = create_app(platform, console_dir=static_dir())
            async with listen(app, port):
                _, user_id = await harness.provision(platform, origin, root_key)
                name, token = await login_cookie(platform, user_id)
                add_login(app, name, token)
                steps = await asyncio.to_thread(drive, args.chrome, f"{origin}/__acceptance__/login",
                                                directory, args.out)
                saved = await verify(origin, name, token)
    verdicts = judge(steps, saved)
    return {"ok": all(v["ok"] for v in verdicts.values()), "model": env["LLM_BOOTSTRAP_MODEL"],
            "verdicts": verdicts, "saved": saved, "steps": steps}


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--model-env", type=Path, required=True, help="私有模型配置（dotenv，权限 600）")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    summary = asyncio.run(run(args))
    (args.out / "entry-chat.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({"ok": summary["ok"], "verdicts": summary["verdicts"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

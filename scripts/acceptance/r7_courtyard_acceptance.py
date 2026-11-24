"""R7 小院真实模型验收：员工发一句话 → 管家派匠人 → 匠人干活 → 唤醒交付；记下小院所需的事实。

复用 R5 真实模型环境（``r5_real_model_harness``）：内存平台、本机沙盒、样例业务包 ``support-desk``
与工单接口替身；模型配置只从 ``--model-env`` 读取，凭据不写进输出。

三个场景（``--scenario``，可重复，默认只跑 solo）：

- ``solo``：一位匠人查工单。检查旁白比例、派工细节（任务说明、验收、结果）、旁白不泄露推理与凭据。
- ``approval``：匠人要改工单优先级（写操作）。检查停下时匠人 span 在等本人、派工细节里有未处理的
  审批项（小院显示「等你盖章」）、批准前工单未被改写；以员工本人身份批准后派工完成、审批项关闭。
- ``consult``：三位匠人会诊。检查有编组 span、至少三次派工挂在编组下、编组细节的步骤是 consult
  （小院请到圆桌）、成员派工细节可读；高分歧时停在等裁决（脚本不代员工裁决）。

每个场景输出每条 span 的元数据与旁白、派工与编组细节，供小院自检页离线重放截图。
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

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SOLO_TEXT = "帮我查一下工单 T-1001 现在什么状态、该多久处理完，查完简单总结给我。"
APPROVAL_TEXT = "把工单 T-1002 的优先级调到 high，改完告诉我结果。"
TERMINAL = ("completed", "failed", "cancelled")


class Run:
    """一次场景的读接口与对账驱动（员工本人身份）。"""

    def __init__(self, platform: Any, client: Any) -> None:
        """平台用于手动对账，客户端以员工身份调用正式接口。"""
        self.platform, self.client = platform, client

    async def get(self, path: str) -> dict[str, Any]:
        """GET 正式接口。"""
        return await self.client._http.request("GET", path)  # noqa: SLF001

    async def send(self, request_id: str, text: str) -> dict[str, Any]:
        """员工对管家说一句话。"""
        return await self.client._http.request(  # noqa: SLF001
            "POST", "/v1/assistant/messages", json={"request_id": request_id, "text": text})

    async def until(self, check: Callable[[], Awaitable[Any]], timeout: float, what: str) -> Any:
        """边对账边等条件成立；超时报错。"""
        deadline = time.monotonic() + timeout
        while True:
            await self.platform.butler.wakeup.run_once()
            found = await check()
            if found:
                return found
            if time.monotonic() > deadline:
                raise TimeoutError(f"等不到：{what}")
            await asyncio.sleep(2)

    async def facts(self, trace_id: str) -> dict[str, Any]:
        """trace 的全部 span 与每次派工、每个编组的细节。"""
        spans = (await self.get(f"/v1/traces/{trace_id}"))["spans"]
        dispatches = {s["ref"]: await self.get(f"/v1/assistant/dispatches/{s['ref']}")
                      for s in spans if s["kind"] == "dispatch" and s.get("ref")}
        groups = {s["ref"]: await self.get(f"/v1/assistant/groups/{s['ref']}")
                  for s in spans if s["kind"] == "group" and s.get("ref")}
        return {"spans": spans, "dispatches": dispatches, "groups": groups}


def says(facts: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """匠人一侧的工具 span 与其中的旁白；以及疑似泄露的旁白。"""
    by_id = {s["span_id"]: s for s in facts["spans"]}

    def under_dispatch(span: dict[str, Any]) -> bool:
        """这个 span 是不是某次派工之下（匠人那边）的。"""
        seen: set[str] = set()
        cur: dict[str, Any] | None = span
        while cur is not None and cur["span_id"] not in seen:
            seen.add(cur["span_id"])
            if cur["kind"] == "dispatch":
                return True
            cur = by_id.get(cur.get("parent_span_id") or "")
        return False

    tools = [s for s in facts["spans"] if s["kind"] == "tool" and under_dispatch(s)]
    leaks = [s["say"] for s in facts["spans"]
             if s.get("say") and ("<think" in s["say"] or "demo-token" in s["say"])]
    return tools, leaks


def summary(facts: dict[str, Any]) -> dict[str, Any]:
    """旁白比例与逐条旁白。"""
    tools, _ = says(facts)
    return {"say_ratio": f"{sum(1 for s in tools if s.get('say'))}/{len(tools)}",
            "says": [{"tool": s["name"], "say": s.get("say")} for s in tools]}


def all_delivered(facts: dict[str, Any]) -> bool:
    """有派工且全部到了终态。"""
    dispatches = [s for s in facts["spans"] if s["kind"] == "dispatch"]
    return bool(dispatches) and all(s["state"] in TERMINAL for s in dispatches)


async def solo(run: Run, timeout: float) -> dict[str, Any]:
    """一位匠人查工单。"""
    receipt = await run.send("r7-solo", SOLO_TEXT)
    trace_id = receipt["trace_id"]

    async def done() -> dict[str, Any] | None:
        facts = await run.facts(trace_id)
        return facts if all_delivered(facts) else None

    facts = await run.until(done, timeout, "派工交付")
    tools, leaks = says(facts)
    detail = next(iter(facts["dispatches"].values()), {})
    checks = {
        "has_butler_root": any(s["kind"] == "butler" and not s.get("parent_span_id") for s in facts["spans"]),
        "has_artisan_tools": bool(tools), "some_tool_has_say": any(s.get("say") for s in tools),
        "no_leak_in_say": not leaks, "detail_has_instruction": bool(detail.get("instruction")),
        "detail_not_echo_tenant": "tenant_id" not in detail and "user_id" not in detail,
        "delivered_with_checks": detail.get("checks") is not None,
    }
    return {"ok": all(checks.values()), "checks": checks, "trace_id": trace_id, **summary(facts), **facts}


async def approval(run: Run, timeout: float, ticket: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """匠人要写工单：停下等盖章 → 本人批准 → 继续完成。"""
    before = await ticket()
    receipt = await run.send("r7-approval", APPROVAL_TEXT)
    trace_id = receipt["trace_id"]

    async def waiting() -> dict[str, Any] | None:
        facts = await run.facts(trace_id)
        open_items = [i for d in facts["dispatches"].values() for i in d["items"]
                      if i["kind"] == "approval" and i["state"] == "open"]
        return facts if open_items else None

    held = await run.until(waiting, timeout, "匠人请求审批")
    held_ticket = await ticket()
    [detail] = list(held["dispatches"].values())
    session_id = next(s["ref"] for s in held["spans"] if s["kind"] == "artisan" and s.get("ref"))
    requests = await run.client.sessions.pending(session_id)
    granted = {str(r["request_id"]): {"granted": True} for r in requests}
    async for _ in run.client.sessions.resume(session_id, granted):
        pass

    async def done() -> dict[str, Any] | None:
        facts = await run.facts(trace_id)
        return facts if all_delivered(facts) else None

    facts = await run.until(done, timeout, "批准后交付")
    after = await ticket()
    final = next(iter(facts["dispatches"].values()))
    checks = {
        "artisan_waiting_for_you": any(s["kind"] == "artisan" and s["state"] == "waiting_input"
                                       for s in held["spans"]),
        "detail_has_open_approval": any(i["kind"] == "approval" and i["state"] == "open"
                                        for i in detail["items"]),
        "write_held_before_approval": held_ticket["priority"] == before["priority"],
        "approved": bool(granted),
        "dispatch_completed": any(s["kind"] == "dispatch" and s["state"] == "completed"
                                  for s in facts["spans"]),
        "approval_closed": not any(i["kind"] == "approval" and i["state"] == "open" for i in final["items"]),
        "ticket_updated": after["priority"] == "high",
        "no_leak_in_say": not says(facts)[1],
    }
    return {"ok": all(checks.values()), "checks": checks, "trace_id": trace_id, "held": held,
            **summary(facts), **facts}


async def consult(run: Run, timeout: float) -> dict[str, Any]:
    """三位匠人会诊：编组在 consult 模式，成员各自干活；高分歧时停在等裁决。"""
    from scripts.acceptance.r5_real_model_harness import CONSULT_TEXT

    receipt = await run.send("r7-consult", CONSULT_TEXT)
    trace_id = receipt["trace_id"]

    async def settled() -> dict[str, Any] | None:
        facts = await run.facts(trace_id)
        groups = list(facts["groups"].values())
        if not groups:
            return None
        group = groups[0]
        waiting = any(v["level"] == "high" and not v["ruled"] and v["pending_id"] for v in group["verdicts"])
        return facts if group["summarized"] or waiting else None

    try:
        facts = await run.until(settled, timeout, "会诊汇总或等裁决")
    except TimeoutError as exc:
        # 超时也留下现场：管家是否编组、各派工停在哪
        last = await run.facts(trace_id)
        return {"ok": False, "error": str(exc), "receipt": receipt, "trace_id": trace_id,
                "states": [(s["kind"], s["name"], s["state"]) for s in last["spans"] if s["kind"] != "tool"],
                **summary(last), **last}
    [group] = list(facts["groups"].values())
    group_span = next(s for s in facts["spans"] if s["kind"] == "group")
    members = [s for s in facts["spans"] if s["kind"] == "dispatch" and s.get("parent_span_id") == group_span["span_id"]]
    checks = {
        "group_is_consult": any(step["mode"] == "consult" for step in group["steps"]),
        "three_members_under_group": len(members) >= 3,
        "member_details_readable": all(m["ref"] in facts["dispatches"] and
                                       facts["dispatches"][m["ref"]].get("instruction") for m in members),
        "no_leak_in_say": not says(facts)[1],
    }
    return {"ok": all(checks.values()), "checks": checks, "trace_id": trace_id,
            "verdicts": group["verdicts"], **summary(facts), **facts}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """起平台与工单替身，开通环境，依次跑选定场景。"""
    import httpx
    from sdk import XuanpuClient
    from server.api.app import create_app

    from scripts.acceptance import r5_real_model_harness as harness
    from scripts.acceptance.finance_browser import free_port, listen

    env = harness.load_model_env(args.model_env)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    tickets = subprocess.Popen([sys.executable, str(ROOT / "examples" / "ticket-api" / "server.py")])  # noqa: S603

    async def ticket() -> dict[str, Any]:
        """直接读工单替身，核对写入是否发生（不经平台）。"""
        async with httpx.AsyncClient(base_url=harness.TICKETS_URL, timeout=10.0) as http:
            response = await http.get("/tickets/T-1002", headers={"Authorization": f"Bearer {harness.TICKETS_TOKEN}"})
            return dict(response.json())

    results: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory() as raw:
            async with AsyncExitStack() as resources:
                platform, root_key = await harness.assemble(Path(raw), env, resources)
                async with listen(create_app(platform), port):
                    key, _ = await harness.provision(platform, base_url, root_key)
                    if "consult" in args.scenario:
                        await harness.publish_variants(base_url, key, Path(raw))
                    print(f"平台 {base_url}，模型 {env['LLM_BOOTSTRAP_MODEL']}", flush=True)
                    for name in args.scenario:
                        user_id = await harness.new_employee(platform, f"R7验收-{name}")
                        async with XuanpuClient(base_url, key, act_as_user=user_id, timeout_seconds=300.0) as client:
                            current = Run(platform, client)
                            try:
                                if name == "solo":
                                    results[name] = await solo(current, args.timeout)
                                elif name == "approval":
                                    results[name] = await approval(current, args.timeout, ticket)
                                else:
                                    results[name] = await consult(current, args.timeout)
                            except Exception as exc:  # 单个场景失败如实记录，不挡后面的场景
                                results[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                        print(name, json.dumps({k: results[name].get(k) for k in ("ok", "checks", "say_ratio")},
                                               ensure_ascii=False), flush=True)
    finally:
        tickets.terminate()
        tickets.wait(timeout=10)
    results["ok"] = all(results[n]["ok"] for n in args.scenario)
    return results


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-env", type=Path, required=True, help="私有模型配置（dotenv，权限 600）")
    parser.add_argument("--out", type=Path, required=True, help="输出目录，每个场景一个 JSON")
    parser.add_argument("--scenario", action="append", choices=("solo", "approval", "consult"))
    parser.add_argument("--timeout", type=float, default=900.0, help="每个场景最长等待秒数")
    args = parser.parse_args()
    args.scenario = args.scenario or ["solo"]
    result = asyncio.run(run(args))
    args.out.mkdir(parents=True, exist_ok=True)
    for name in args.scenario:
        (args.out / f"{name}.json").write_text(json.dumps(result[name], ensure_ascii=False, indent=2))
    for name in args.scenario:
        print(name, json.dumps({k: result[name].get(k) for k in ("ok", "checks", "say_ratio", "says", "verdicts", "error", "states")},
                               ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

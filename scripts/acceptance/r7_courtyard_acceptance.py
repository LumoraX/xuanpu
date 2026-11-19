"""R7 小院真实模型验收：员工发一句话 → 管家派匠人 → 匠人干活 → 唤醒交付；记下小院所需的事实。

复用 R5 真实模型环境（``r5_real_model_harness``）：内存平台、本机沙盒、样例业务包 ``support-desk``
与工单接口替身；模型配置只从 ``--model-env`` 读取，凭据不写进输出。

检查：
- trace 里有管家根、派工、匠人执行与工具 span；
- 匠人的工具 span 带旁白（``say``）的比例（模型是否照约定先说一句）；
- ``GET /v1/assistant/dispatches/{id}`` 给出任务说明、验收与结果摘录；
- 推理与工具参数没有进 trace（旁白里不含 ``<think>``、不含工单接口凭据）。

输出 JSON 到 ``--out``：每条 span 的元数据与旁白、派工细节，供小院自检页离线重放截图。
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
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REQUEST = "帮我查一下工单 T-1001 现在什么状态、该多久处理完，查完简单总结给我。"


async def wait_delivered(platform: Any, client: Any, trace_id: str, timeout: float) -> list[dict[str, Any]]:
    """反复对账唤醒，直到这条 trace 的派工都交付（或超时）；返回最终的 span 列表。"""
    deadline = time.monotonic() + timeout
    while True:
        await platform.butler.wakeup.run_once()
        tree = await client._http.request("GET", f"/v1/traces/{trace_id}")  # noqa: SLF001
        dispatches = [s for s in tree["spans"] if s["kind"] == "dispatch"]
        if dispatches and all(s["state"] in ("completed", "failed", "cancelled") for s in dispatches):
            return list(tree["spans"])
        if time.monotonic() > deadline:
            return list(tree["spans"])
        await asyncio.sleep(2)


async def scenario(platform: Any, client: Any, timeout: float) -> dict[str, Any]:
    """发一句话、等交付、取 trace 与派工细节，逐项检查。"""
    receipt = await client._http.request("POST", "/v1/assistant/messages",  # noqa: SLF001
                                         json={"request_id": "r7-courtyard-1", "text": REQUEST})
    trace_id = receipt.get("trace_id")
    if not trace_id:
        return {"ok": False, "receipt": receipt, "error": "回执没有 trace_id"}
    spans = await wait_delivered(platform, client, trace_id, timeout)
    by_id = {s["span_id"]: s for s in spans}
    details = {}
    for span in spans:
        if span["kind"] == "dispatch" and span.get("ref"):
            details[span["ref"]] = await client._http.request(  # noqa: SLF001
                "GET", f"/v1/assistant/dispatches/{span['ref']}")

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

    artisan_tools = [s for s in spans if s["kind"] == "tool" and under_dispatch(s)]
    said = [s for s in artisan_tools if s.get("say")]
    leaks = [s["say"] for s in spans if s.get("say") and ("<think" in s["say"] or "demo-token" in s["say"])]
    detail = next(iter(details.values()), {})
    checks = {
        "has_butler_root": any(s["kind"] == "butler" and not s.get("parent_span_id") for s in spans),
        "has_dispatch": bool(details),
        "has_artisan_tools": bool(artisan_tools),
        "some_tool_has_say": bool(said),
        "no_leak_in_say": not leaks,
        "detail_has_instruction": bool(detail.get("instruction")),
        "detail_not_echo_tenant": "tenant_id" not in detail and "user_id" not in detail,
        "delivered_with_checks": detail.get("checks") is not None,
    }
    return {
        "ok": all(checks.values()), "checks": checks, "trace_id": trace_id,
        "say_ratio": f"{len(said)}/{len(artisan_tools)}",
        "says": [{"tool": s["name"], "say": s.get("say")} for s in artisan_tools],
        "spans": spans, "dispatches": details,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """起平台与工单替身，开通环境，跑一次真实请求。"""
    from xuanpu_sdk import XuanpuClient
    from xuanpu_server.api.app import create_app

    from scripts.acceptance import r5_real_model_harness as harness
    from scripts.acceptance.finance_browser import free_port, listen

    env = harness.load_model_env(args.model_env)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    tickets = subprocess.Popen([sys.executable, str(ROOT / "examples" / "ticket-api" / "server.py")])  # noqa: S603
    try:
        with tempfile.TemporaryDirectory() as raw:
            async with AsyncExitStack() as resources:
                platform, root_key = await harness.assemble(Path(raw), env, resources)
                async with listen(create_app(platform), port):
                    key, user_id = await harness.provision(platform, base_url, root_key)
                    print(f"平台 {base_url}，模型 {env['LLM_BOOTSTRAP_MODEL']}", flush=True)
                    async with XuanpuClient(base_url, key, act_as_user=user_id,
                                            timeout_seconds=300.0) as client:
                        return await scenario(platform, client, args.timeout)
    finally:
        tickets.terminate()
        tickets.wait(timeout=10)


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-env", type=Path, required=True, help="私有模型配置（dotenv，权限 600）")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600.0, help="等匠人交付的最长秒数")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: result.get(k) for k in ("ok", "checks", "say_ratio", "says")}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()

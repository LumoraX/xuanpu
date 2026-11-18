"""R6 管家常在线真实模型验收：连续交代、进行中不阻塞、结果主动回到主对话；预算随任务提高；一轮到时限正常收尾。

平台与模型同 ``r5_real_model_harness.py``（模型只从 ``--model-env`` 的私有 dotenv 读）。为了可靠看到
「进行中」，回复窗口调到 3 秒。三个场景各用一名员工，互不干扰：

1. 连续三句：慢的（长分析）、快的（问答）、派工的（查工单）。快的必须在慢的仍在进行时答复；
   慢的那句的回复与派工的匠人结果都要主动出现在主对话。
2. 预算：每次请求起始只允许 1 位匠人，要求三位匠人会诊；管家须用 ``raise_budget`` 说明理由提高
   并成功安排编组（审计 ``butler_budget_raised``）。
3. 时限：一轮 10 秒，要求一份很长的方案；须正常收尾（完成、或延长、或说明先停下），不得 503。
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

SLOW = ("请帮我系统梳理客户投诉处理流程的改进方案：先列出常见问题类型，再给每类的处理时限、"
        "升级条件、责任角色和回访要求，最后给出一页纸的执行清单。")
QUICK = "今天是星期几？"
DISPATCH = "帮我查一下工单 T-1001 的处理进度"
CONSULT = ("请安排三位匠人会诊：工单 T-1001 该不该升级处理？请一线客服、二线技术和质检各自独立给出结论，"
           "全部给完后再比对，不要只交给一位。")
LONG = "请自己写一份 3000 字的客户服务年度改进方案，分章节写完整，不要交给别人。"
DEADLINE = 600.0


async def poll(check: Any, timeout: float, what: str) -> Any:
    """轮询直到 ``check()`` 返回真值；超时报错说明在等什么。"""
    end = time.monotonic() + timeout
    while True:
        value = await check()
        if value:
            return value
        if time.monotonic() > end:
            raise TimeoutError("等待超时：" + what)
        await asyncio.sleep(3)


async def conversation(platform: Any, who: Any) -> list[dict[str, Any]]:
    """员工主对话（最近 100 条）。"""
    return list(await platform.workspace.messages(who, limit=100, latest=True))


async def scenario_concurrent(platform: Any, client: Any, who: Any) -> dict[str, Any]:
    """连续三句：各自按自己的进度回到主对话，快的不等慢的；派工确实发出，匠人结果主动回报。"""
    from xuanpu_server.tracing.models import TraceOwner

    start = time.monotonic()
    receipts = {}
    for request_id, text in (("r6-slow", SLOW), ("r6-quick", QUICK), ("r6-dispatch", DISPATCH)):
        receipts[request_id] = await client.send_to_butler(text, request_id=request_id)
    sent = time.monotonic() - start
    arrived: dict[str, float] = {}

    async def all_replied() -> bool:
        for message in await conversation(platform, who):
            key = str(message["request_id"]).removesuffix(":assistant")
            if key in receipts and key not in arrived:
                arrived[key] = round(time.monotonic() - start, 1)
        return len(arrived) == len(receipts)

    async def artisan_reported() -> bool:
        return any(m["role"] == "assistant" and str(m["request_id"]).startswith("butler:")
                   and "T-1001" in m["text"] for m in await conversation(platform, who))

    await poll(all_replied, DEADLINE, "三句话的回复")
    trace = receipts["r6-dispatch"]["trace_id"]
    dispatched = await platform.store.dispatch_store.list_for_trace(
        TraceOwner(tenant_id=who.tenant_id, user_id=who.user_id), trace)
    if dispatched:
        await poll(artisan_reported, DEADLINE, "匠人结果回到主对话")
    checks = {
        "three_sent_without_waiting": sent < 3 * 3.0 + 6,  # 每句至多等 3 秒窗口，加网络余量
        "slow_returned_running": receipts["r6-slow"].get("state") == "running",
        "quick_not_blocked_by_slow": arrived["r6-quick"] <= arrived["r6-slow"],
        "dispatched": len(dispatched) == 1,
        "artisan_result_delivered": await artisan_reported(),
    }
    return {"ok": all(checks.values()), "checks": checks, "sent_seconds": round(sent, 1),
            "arrived_seconds": arrived,
            "states": {k: v.get("state") for k, v in receipts.items()}}


async def scenario_budget(platform: Any, client: Any, who: Any) -> dict[str, Any]:
    """起始只允许 1 位匠人：三位匠人会诊须先提高预算。"""
    from xuanpu_server.controlplane.models import AuditQuery

    receipt = await client.send_to_butler(CONSULT, request_id="r6-budget")

    async def finished() -> dict[str, Any] | None:
        if receipt.get("state") == "done":
            return receipt
        again = await client.send_to_butler(CONSULT, request_id="r6-budget")
        return again if again.get("state") == "done" else None

    final = await poll(finished, DEADLINE, "会诊这一轮结束")
    raises = await platform.store.query_audit(who.tenant_id, AuditQuery(
        kind="butler_budget_raised", user_id=who.user_id, limit=20))
    data = [dict(r.data) for r in raises]
    checks = {"budget_raised": any(d.get("budget") == "artisans" and d.get("limit", 0) >= 3 for d in data),
              "group_arranged": bool(final.get("groups"))}
    return {"ok": all(checks.values()), "checks": checks, "raises": data, "message": final.get("message")}


async def scenario_deadline(platform: Any, client: Any, who: Any) -> dict[str, Any]:
    """一轮 10 秒：须正常收尾，不得 503。"""
    from xuanpu_server.controlplane.models import AuditQuery

    receipt = await client.send_to_butler(LONG, request_id="r6-long")

    async def replied() -> str | None:
        for message in await conversation(platform, who):
            if message["request_id"] == "r6-long:assistant":
                return str(message["text"])
        return None

    text = await poll(replied, DEADLINE, "长方案这一轮的回复")
    extended = await platform.store.query_audit(who.tenant_id, AuditQuery(
        kind="butler_turn_extended", user_id=who.user_id, limit=20))
    failed = "没有处理完" in text
    return {"ok": not failed, "receipt_state": receipt.get("state"), "extended": len(extended),
            "stopped_at_deadline": "已到时限先停下" in text, "reply_head": text[:200]}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """起平台（回复窗口 3 秒）与工单替身，依次跑三个场景。"""
    from xuanpu_sdk import XuanpuClient
    from xuanpu_server.api.app import create_app

    from scripts.acceptance import r5_real_model_harness as harness
    from scripts.acceptance.finance_browser import free_port, listen

    env = harness.load_model_env(args.model_env)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    tickets = subprocess.Popen([sys.executable, str(ROOT / "examples" / "ticket-api" / "server.py")])  # noqa: S603
    results: dict[str, Any] = {"model": env["LLM_BOOTSTRAP_MODEL"]}
    try:
        with tempfile.TemporaryDirectory() as raw:
            async with AsyncExitStack() as resources:
                platform, root_key = await harness.assemble(Path(raw), env, resources)
                async with listen(create_app(platform), port):
                    key, _ = await harness.provision(platform, base_url, root_key)
                    await harness.publish_variants(base_url, key, Path(raw))
                    base = platform.butler_config.model_dump()
                    for name, scenario, overrides in (
                        ("concurrent", scenario_concurrent, {"reply_window_seconds": 3.0}),
                        ("budget", scenario_budget, {"max_artisans_per_trace": 1}),
                        ("deadline", scenario_deadline, {"turn_seconds": args.deadline_seconds, "reply_window_seconds": 3.0}),
                    ):
                        if args.only and name not in args.only:
                            continue
                        platform.butler_config = type(platform.butler_config)(**{**base, **overrides})
                        user_id = await harness.new_employee(platform, f"R6验收-{name}")
                        who = harness_principal(user_id)
                        async with XuanpuClient(base_url, key, act_as_user=user_id,
                                                timeout_seconds=120.0) as client:
                            try:
                                results[name] = await scenario(platform, client, who)
                            except Exception as exc:  # 单个场景失败如实记录，不挡后面的场景
                                results[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                        print(name, json.dumps(results[name].get("checks", results[name]),
                                               ensure_ascii=False)[:300], flush=True)
    finally:
        tickets.terminate()
        tickets.wait(timeout=10)
    results["ok"] = all(results[n]["ok"] for n in ("concurrent", "budget", "deadline") if n in results)
    return results


def harness_principal(user_id: str) -> Any:
    """验收员工的身份（成员）。"""
    from xuanpu_server.tenancy import Principal

    return Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id=user_id)


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-env", type=Path, required=True, help="私有模型配置（dotenv，权限 600）")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", action="append", choices=("concurrent", "budget", "deadline"),
                        help="只跑这些场景（可重复）；缺省全跑")
    parser.add_argument("--deadline-seconds", type=float, default=10.0,
                        help="时限场景的一轮时限（秒）；短于一次模型采样时管家来不及延长")
    args = parser.parse_args()
    summary = asyncio.run(run(args))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "r6.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"ok={summary['ok']}")


if __name__ == "__main__":
    main()

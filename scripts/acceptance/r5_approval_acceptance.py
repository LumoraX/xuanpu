"""R5 真实模型验收：管家派工 → 匠人要写工单 → 员工审批 → 匠人继续 → 管家交付，并核对工单确已改写。

不读环境变量：平台地址、凭据文件、代表的员工与工单接口地址由命令行给出。只调用正式接口：
``POST /v1/assistant/messages`` 提交请求，``GET /v1/assistant/pending`` 等审批提示，
``GET /v1/sessions/{id}/pending`` 与 ``POST /v1/sessions/{id}/resume`` 以员工本人身份批准，
``GET /v1/traces/{id}/waterfall`` 看派工终态，``GET /v1/workspace/messages`` 看管家交付到主对话。
批准前先核对工单未被改写（审批真的挡在写操作前）。结果写成 JSON 供验收记录引用。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
from xuanpu_sdk import XuanpuClient

# 管家一轮在服务端最长 90 秒；客户端须等得比它久
CLIENT_TIMEOUT = 120.0
TICKET = "T-1002"
TEXT = f"把工单 {TICKET} 的优先级调到 high，改完告诉我结果。"
POLL_SECONDS = 3.0


async def ticket(tickets_url: str, token: str) -> dict[str, Any]:
    """直接读工单替身，核对写入是否发生（不经平台）。"""
    async with httpx.AsyncClient(base_url=tickets_url, timeout=10.0) as http:
        response = await http.get(f"/tickets/{TICKET}", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return dict(response.json())


async def until(check: Any, deadline: float, what: str) -> Any:
    """轮询直到 ``check()`` 返回真值；超时报错并说明在等什么。"""
    while True:
        value = await check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"等待超时：{what}")
        await asyncio.sleep(POLL_SECONDS)


async def main_conversation(args: argparse.Namespace, key: str) -> list[dict[str, Any]]:
    """员工主对话最近 10 条（SDK 尚无这个读取方法，直接调正式接口，身份与 SDK 相同）。"""
    headers = {"Authorization": f"Bearer {key}", "X-Xuanpu-Act-As": args.act_as}
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0, headers=headers) as http:
        response = await http.get("/v1/workspace/messages", params={"latest": "true", "limit": 10})
        response.raise_for_status()
        return list(response.json()["items"])


def dispatch_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """瀑布里唯一的派工行。"""
    found = [r for r in rows if r.get("kind") == "dispatch"]
    return found[0] if len(found) == 1 else None


async def approve(client: XuanpuClient, session_id: str) -> list[str]:
    """以员工本人身份批准会话里全部待审批请求，读完续跑事件流；返回批准的请求标识。"""
    requests = await client.sessions.pending(session_id)
    granted = {str(r["request_id"]): {"granted": True} for r in requests}
    async for _ in client.sessions.resume(session_id, granted):
        pass
    return list(granted)


async def run(args: argparse.Namespace, key: str) -> dict[str, Any]:
    """提交请求、等审批、批准、等交付，汇总验收结论。"""
    deadline = time.monotonic() + args.timeout
    before = await ticket(args.tickets_url, args.tickets_token)
    async with XuanpuClient(args.base_url, key, act_as_user=args.act_as,
                            timeout_seconds=CLIENT_TIMEOUT) as client:
        receipt = await client.send_to_butler(TEXT, request_id=f"r5approval-{int(time.time())}")
        if len(receipt.get("dispatches", [])) != 1:
            return {"ok": False, "reason": "管家没有派给一位匠人", "receipt": receipt}
        trace_id = receipt["trace_id"]

        async def approval_item() -> dict[str, Any] | None:
            items = await client.list_butler_pending()
            return next((i for i in items if i["kind"] == "approval"), None)

        item = await until(approval_item, deadline, "匠人请求审批")
        held = await ticket(args.tickets_url, args.tickets_token)
        granted = await approve(client, item["host_ref"])

        async def delivered() -> dict[str, Any] | None:
            row = dispatch_row((await client.trace_waterfall(trace_id))["rows"])
            return row if row and row.get("duration_ms") is not None else None

        row = await until(delivered, deadline, "派工结束")
        pending_after = await client.list_butler_pending()
        messages = await main_conversation(args, key)
    after = await ticket(args.tickets_url, args.tickets_token)
    still_open = [i for i in pending_after if i["pending_id"] == item["pending_id"]]
    texts = [str(m.get("text", "")) for m in messages]
    checks = {
        "approval_question_points_to_confirmations": "待确认" in item["question"],
        "write_held_before_approval": held["priority"] == before["priority"],
        "approved_requests": len(granted) >= 1,
        "dispatch_completed": row.get("state") == "completed",
        "ticket_updated": after["priority"] == "high",
        "approval_item_resolved": not still_open,
        "delivered_to_main_conversation": any(TICKET in t for t in texts[-3:]),
    }
    return {"ok": all(checks.values()), "checks": checks, "receipt_message": receipt.get("message"),
            "approval_question": item["question"], "ticket_before": before, "ticket_after": after,
            "dispatch_row": row, "last_messages": texts[-3:]}


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key-file", required=True, help="权限 600 的凭据文件，只含一行 API key")
    parser.add_argument("--act-as", required=True, help="代表的员工标识")
    parser.add_argument("--tickets-url", required=True, help="样例工单接口地址")
    parser.add_argument("--tickets-token", required=True, help="样例工单接口凭据（替身用 demo-token）")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    key = Path(args.key_file).read_text().strip()
    summary = asyncio.run(run(args, key))
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"ok={summary['ok']} checks={json.dumps(summary.get('checks'), ensure_ascii=False)}")


if __name__ == "__main__":
    main()

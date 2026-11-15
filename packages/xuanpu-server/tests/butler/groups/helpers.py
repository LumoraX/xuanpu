"""编组测试的共用构造：提交计划、反复唤醒直到条件成立、读汇总消息。"""

from __future__ import annotations

import asyncio
from typing import Any

from xuanpu_server.tracing.models import TraceOwner

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
WAKE = "artisan_round_finished"


def member(marker: str, brief: str = "") -> dict[str, Any]:
    """一个成员：样例匠人，任务说明以标记开头（脚本化模型按标记回复）。"""
    return {"package": "support-desk", "entry": "triage", "brief": f"{marker} {brief}".strip()}


def plan(*steps: dict[str, Any], title: str = "工单排查") -> dict[str, Any]:
    """一份计划。"""
    return {"title": title, "steps": list(steps)}


async def submit(butler: dict[str, Any], step: Any, request_id: str,
                 group_plan: dict[str, Any]) -> dict[str, Any]:
    """员工说一句话，管家提交编组计划。"""
    butler["world"].models.script(
        step(calls=[("g1", "dispatch_group", {"plan": group_plan})]),
        step(text="已安排几位匠人一起处理，全部结束后统一告诉你。"))
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": request_id, "text": "一起查一下"})
    assert response.status_code == 200, response.text
    return dict(response.json())


async def until(butler: dict[str, Any], predicate: Any, timeout: float = 30.0) -> None:
    """反复唤醒（对账、处理、推进编组），直到条件成立。"""
    wakeup = butler["world"].platform.butler.wakeup
    async with asyncio.timeout(timeout):
        while not await predicate():
            await wakeup.run_once()
            await asyncio.sleep(0.05)


async def group(butler: dict[str, Any], group_id: str) -> Any:
    """读编组。"""
    return await butler["world"].platform.butler.groups.get(ALICE, group_id)


async def summaries(butler: dict[str, Any]) -> list[dict[str, Any]]:
    """主对话里的编组汇总消息。"""
    found = await butler["world"].platform.workspace.messages(
        butler["alice"], limit=100, latest=True, private_only=True)
    return [m for m in found if str(m["request_id"]).endswith(":summary")]


async def member_dispatches(butler: dict[str, Any], trace_id: str) -> list[Any]:
    """这条 trace 下的成员派工。"""
    records = await butler["world"].platform.dispatches.list_for_trace(ALICE, trace_id)
    return [r for r in records if r.group_id is not None]

"""回执与一轮解耦（R6-A，ADR 0020）：慢的一轮先回「进行中」，员工可以接着说下一句，结果进主对话。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from xuanpu_server.butler import service as butler_service
from xuanpu_server.errors import ConflictError

from .conftest import enable


async def _say(butler: dict[str, Any], text: str, request_id: str) -> dict[str, Any]:
    """以 Alice 身份对管家说一句话。"""
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": request_id, "text": text})
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _replies(butler: dict[str, Any], request_id: str) -> list[str]:
    """主对话里某句话对应的管家回复。"""
    found = await butler["world"].platform.workspace.messages(butler["alice"], limit=100, latest=True)
    return [m["text"] for m in found if m["role"] == "assistant"
            and str(m.get("request_id", "")).startswith(request_id)]


async def _eventually(check: Any, timeout: float = 10.0) -> None:
    """等条件成立。"""
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.02)


async def test_fast_turn_still_returns_the_full_receipt(butler: dict[str, Any], step: Any) -> None:
    """窗口内办完：照旧返回完整回执，标 state=done。"""
    butler["world"].models.script(step(text="你好。"))
    receipt = await _say(butler, "你好", "f1")
    assert receipt["state"] == "done" and receipt["message"] == "你好。"


async def test_slow_turn_returns_running_and_employee_can_keep_talking(
    butler: dict[str, Any], step: Any,
) -> None:
    """慢的一轮先回进行中；这期间再说一句立即处理；慢的那轮结束后回复进主对话，同标识重试不重跑。"""
    enable(butler["world"], reply_window_seconds=1.0)
    world = butler["world"]
    gate = asyncio.Event()
    world.models.script_for("整理全年的报表", step(text="全年报表整理好了。", wait=gate))
    world.models.script_for("今天周几", step(text="今天周二。"))

    slow = await _say(butler, "整理全年的报表", "slow")
    assert slow["state"] == "running" and slow["trace_id"].startswith("trc")
    quick = await _say(butler, "今天周几", "quick")
    assert quick["state"] == "done" and quick["message"] == "今天周二。"
    again = await _say(butler, "整理全年的报表", "slow")
    assert again == slow  # 进行中重试：同一 trace，不再跑一轮

    gate.set()

    async def delivered() -> bool:
        return await _replies(butler, "slow") == ["全年报表整理好了。"]

    await _eventually(delivered)
    final = await _say(butler, "整理全年的报表", "slow")
    assert final["state"] == "done" and final["trace_id"] == slow["trace_id"]
    # 没有重跑：慢的那一步只被取用一次，也没有哪一轮落到「脚本已用完」
    assert world.models.keyed["整理全年的报表"] == []
    replies = await world.platform.workspace.messages(butler["alice"], limit=100, latest=True)
    assert not any("脚本已用完" in m["text"] for m in replies)


async def test_running_turn_keeps_its_claim_renewed(
    butler: dict[str, Any], step: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一轮跑得比一次租约长：持有者续租，别的处理者认领不到（不会重复处理）。"""
    monkeypatch.setattr(butler_service, "_RENEW_SECONDS", 0.05)
    monkeypatch.setattr(butler_service, "_LEASE_SECONDS", 0.2)
    enable(butler["world"], reply_window_seconds=1.0)
    world = butler["world"]
    gate = asyncio.Event()
    world.models.script_for("盘点库存", step(text="盘点完了。", wait=gate))
    receipt = await _say(butler, "盘点库存", "long")
    assert receipt["state"] == "running"
    await asyncio.sleep(0.6)  # 远长于 0.2 秒租约
    with pytest.raises(ConflictError):
        await world.platform.workspace.claim_route(butler["alice"], "butler:long", {
            "text": "盘点库存", "timezone": "Asia/Shanghai", "input_files": []})
    gate.set()

    async def delivered() -> bool:
        return await _replies(butler, "long") == ["盘点完了。"]

    await _eventually(delivered)


async def test_failure_after_window_is_written_to_the_conversation(
    butler: dict[str, Any], step: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """员工已拿到进行中回执后这一轮失败：主对话写明没处理完、原话已保留。"""
    enable(butler["world"], reply_window_seconds=1.0)
    world = butler["world"]
    gate = asyncio.Event()
    world.models.script_for("汇总合同", step(text="", wait=gate))
    engines = world.platform.butler.engines
    original = engines.run

    async def failing(turn: Any, payload: str) -> str:
        await gate.wait()
        raise RuntimeError("模型网关断开")

    monkeypatch.setattr(engines, "run", failing)
    receipt = await _say(butler, "汇总合同", "broken")
    assert receipt["state"] == "running"
    gate.set()

    async def reported() -> bool:
        replies = await _replies(butler, "broken")
        return len(replies) == 1 and "原话已保留" in replies[0]

    await _eventually(reported)
    monkeypatch.setattr(engines, "run", original)


async def test_close_cancels_running_turns(butler: dict[str, Any], step: Any) -> None:
    """平台关闭：进行中的轮次被取消，不写半截回复。"""
    enable(butler["world"], reply_window_seconds=1.0)
    world = butler["world"]
    world.models.script_for("写年终总结", step(text="写好了。", wait=asyncio.Event()))
    receipt = await _say(butler, "写年终总结", "cut")
    assert receipt["state"] == "running"
    await world.platform.butler.close()
    assert await _replies(butler, "cut") == []

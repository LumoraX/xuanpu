"""会诊端到端：成员互不可见、全部完成后比对；低分歧综合、高分歧交员工裁决、敏感话题强制交员工；
裁决幂等、取消关闭裁决项、重启不重复裁决项与消息。"""

from __future__ import annotations

import time
from typing import Any

from xuanpu_server.butler.groups.runner import GroupRunner

from ..conftest import enable
from .helpers import ALICE, WAKE, group, member, member_dispatches, plan, submit, summaries, until

COMPARE = "consult_compare"


def _rate(step: Any, **rating: Any) -> list[Any]:
    """比对一轮的脚本：登记评级，再说一句话。"""
    return [step(calls=[("rate1", "rate_divergence", rating)]), step(text="已比对。")]


async def _consult(butler: dict[str, Any], step: Any, request_id: str,
                   replies: dict[str, str]) -> tuple[str, str]:
    """提交一步会诊：每位成员按标记给出结论；返回 (编组, trace)。"""
    world = butler["world"]
    for marker, text in replies.items():
        world.models.script_for(marker, step(text=text))
    world.models.script_for(WAKE, *[step(text="结果回答了原请求。") for _ in replies])
    receipt = await submit(butler, step, request_id, plan(
        {"mode": "consult", "members": [member(m, "这个故障该怎么处理？") for m in replies]},
        title="故障处置会诊"))
    [group_id] = receipt["groups"]
    return group_id, receipt["trace_id"]


async def _summarized(butler: dict[str, Any], group_id: str) -> bool:
    """编组已汇总。"""
    return bool((await group(butler, group_id)).summarized)


async def test_low_divergence_synthesis_keeps_originals(butler: dict[str, Any], step: Any) -> None:
    """低分歧：管家综合并注明依据；每位成员原结论都在；成员互相看不到对方。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for(COMPARE, *_rate(step, level="low", synthesis="都建议先重启服务",
                                            basis=[1, 2]))
    group_id, trace_id = await _consult(butler, step, "c1", {
        "INSTR-C1": "C1：先重启服务。", "INSTR-C2": "C2：建议重启服务后观察。"})
    launched = await member_dispatches(butler, trace_id)
    assert {d.state for d in launched} == {"running"}
    first = next(d for d in launched if "INSTR-C1" in d.instruction)
    assert "INSTR-C2" not in first.instruction
    await until(butler, lambda: _summarized(butler, group_id))
    record = await group(butler, group_id)
    verdict = record.verdict(0)
    assert verdict is not None and (verdict.level, verdict.rated_level) == ("low", "low")
    [message] = await summaries(butler)
    text = message["text"]
    assert "管家综合（依据：成员1、成员2）：都建议先重启服务" in text
    assert "C1：先重启服务。" in text and "C2：建议重启服务后观察。" in text
    assert await world.platform.store.wake_store.list_pending(ALICE) == ()


async def test_high_divergence_goes_to_employee_then_ruling(
    butler: dict[str, Any], step: Any,
) -> None:
    """高分歧：并排列出结论、等员工裁决；裁决幂等，不同内容冲突；裁决后才收口。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for(COMPARE, *_rate(step, level="high"))
    group_id, _ = await _consult(butler, step, "c2", {
        "INSTR-H1": "H1：必须立即回滚。", "INSTR-H2": "H2：不要回滚，打补丁即可。"})

    async def ruling_open() -> bool:
        verdict = (await group(butler, group_id)).verdict(0)
        return verdict is not None and verdict.pending_id is not None

    await until(butler, ruling_open)
    listed = (await butler["http"].get("/v1/assistant/pending")).json()["items"]
    [item] = [i for i in listed if i["kind"] == "ruling"]
    assert "H1：必须立即回滚。" in item["question"] and "H2：不要回滚" in item["question"]
    for _ in range(3):
        await world.platform.butler.wakeup.run_once()
    assert not (await group(butler, group_id)).summarized
    path = f"/v1/assistant/pending/{item['pending_id']}/answer"
    body = {"request_id": "rule-1", "text": "按 H2 打补丁"}
    assert (await butler["http"].post(path, json=body)).status_code == 200
    assert (await butler["http"].post(path, json=body)).status_code == 200
    other = await butler["http"].post(path, json={"request_id": "rule-2", "text": "回滚"})
    assert other.status_code == 409
    await until(butler, lambda: _summarized(butler, group_id))
    [message] = await summaries(butler)
    assert "你的裁决：按 H2 打补丁" in message["text"]
    assert "H1：必须立即回滚。" in message["text"]
    rulings = [m for m in await world.platform.workspace.messages(
        butler["alice"], limit=100, latest=True, private_only=True)
        if ":ruling:" in str(m["request_id"])]
    assert len(rulings) == 1


async def test_money_forces_employee_ruling(butler: dict[str, Any], step: Any) -> None:
    """模型评低，但结论涉及金额：仍交员工裁决。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for(COMPARE, *_rate(step, level="low", synthesis="都同意退款",
                                            basis=[1, 2]))
    group_id, _ = await _consult(butler, step, "c3", {
        "INSTR-M1": "M1：同意退款 500 元。", "INSTR-M2": "M2：同意退款。"})

    async def ruling_open() -> bool:
        verdict = (await group(butler, group_id)).verdict(0)
        return verdict is not None and verdict.pending_id is not None

    await until(butler, ruling_open)
    verdict = (await group(butler, group_id)).verdict(0)
    assert verdict is not None
    assert (verdict.level, verdict.reasons, verdict.rated_level) == ("high", ("amount",), "low")


async def test_cancel_while_waiting_closes_ruling(butler: dict[str, Any], step: Any) -> None:
    """等裁决时取消编组：裁决项关闭，编组记为取消，之后不能再裁决。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for(COMPARE, *_rate(step, level="high"))
    group_id, _ = await _consult(butler, step, "c4", {
        "INSTR-K1": "K1：方案甲。", "INSTR-K2": "K2：方案乙。"})

    async def ruling_open() -> bool:
        verdict = (await group(butler, group_id)).verdict(0)
        return verdict is not None and verdict.pending_id is not None

    await until(butler, ruling_open)
    await world.platform.butler.groups.cancel(ALICE, group_id)
    await until(butler, lambda: _summarized(butler, group_id))
    assert (await group(butler, group_id)).state == "cancelled"
    assert await world.platform.store.wake_store.list_pending(ALICE) == ()
    verdict = (await group(butler, group_id)).verdict(0)
    assert verdict is not None and verdict.pending_id is not None
    response = await butler["http"].post(f"/v1/assistant/pending/{verdict.pending_id}/answer",
                                         json={"request_id": "late", "text": "甲"})
    assert response.status_code == 409


async def test_crash_after_verdict_before_ruling_item(
    butler: dict[str, Any], step: Any, monkeypatch: Any,
) -> None:
    """比对结论已保存、建裁决项前终止：重启后只有一个裁决项和一条裁决消息，比对不重跑。"""
    world = butler["world"]
    enable(world, wake_interval_seconds=300)
    world.models.script_for(COMPARE, *_rate(step, level="high"))
    store = world.platform.butler.group_store
    original = store.save
    state = {"armed": True}

    async def save(owner: Any, record: Any, *, expected_version: int) -> Any:
        """保存带裁决项标识的裁决时「终止」一次（裁决项与消息已写）。"""
        verdict = record.verdict(0)
        if state["armed"] and verdict is not None and verdict.pending_id is not None:
            state["armed"] = False
            raise RuntimeError("模拟终止")
        return await original(owner, record, expected_version=expected_version)

    monkeypatch.setattr(store, "save", save)
    group_id, _ = await _consult(butler, step, "c5", {
        "INSTR-X1": "X1：方案甲。", "INSTR-X2": "X2：方案乙。"})

    async def crashed() -> bool:
        return not state["armed"]

    await until(butler, crashed)
    butler_service = world.platform.butler
    fresh = GroupRunner(world.platform, butler_service.dispatcher, butler_service.group_store,
                        now=time.time)
    await fresh.advance(ALICE, group_id)
    await fresh.advance(ALICE, group_id)
    items = [i for i in await world.platform.store.wake_store.list_pending(ALICE)
             if i.kind == "ruling"]
    assert len(items) == 1
    verdict = (await group(butler, group_id)).verdict(0)
    assert verdict is not None and verdict.pending_id == items[0].pending_id
    rulings = [m for m in await world.platform.workspace.messages(
        butler["alice"], limit=100, latest=True, private_only=True)
        if ":ruling:" in str(m["request_id"])]
    assert len(rulings) == 1

"""编组接力自动过程产物冻结与私域 capture_bundle/merge_people 完整真撤回测试。"""

from __future__ import annotations

from typing import Any

from server.engine.butler.personal_actions import execute_personal_action, undo_executed_action
from server.engine.butler.turn import ButlerTurn
from server.engine.tracing.models import TraceOwner


async def test_capture_bundle_update_true_undo_and_merge_people_undo(
    butler: dict[str, Any],
) -> None:
    """capture_bundle 更新已有档案时支持还原修改前快照；merge_people 支持一键撤回拆分。"""
    platform = butler["world"].platform
    alice = butler["alice"]
    turn = ButlerTurn(who=alice, trace_id="trc_undo_p", request_id="req_undo_p", timezone="Asia/Shanghai")

    p1 = await platform.people.create_person(
        alice, {"name": "王建国", "organization": "旧公司", "tags": ["老客户"]}, "seed_p1"
    )
    p2 = await platform.people.create_person(
        alice, {"name": "王建国（副本）", "organization": "旧公司"}, "seed_p2"
    )

    # 1. capture_bundle 更新已有 p1 -> 撤回后应完整恢复为 "旧公司"
    res = await execute_personal_action(
        platform,
        turn,
        {
            "action": "capture_bundle",
            "bundle": {
                "persons": [
                    {
                        "action": "update",
                        "person_id": p1["id"],
                        "profile": {"name": "王建国", "organization": "新公司"},
                    }
                ]
            },
        },
        now=1_700_000_000.0,
    )
    assert res["ok"] is True
    updated_p1 = (await platform.people.get_person(alice, p1["id"]))["person"]
    assert updated_p1["profile"]["organization"] == "新公司"

    undo_res = await undo_executed_action(platform, alice, turn.executed_actions[-1])
    assert undo_res["undone"] >= 1
    restored_p1 = (await platform.people.get_person(alice, p1["id"]))["person"]
    assert restored_p1["profile"]["organization"] == "旧公司"

    # 2. merge_people 软合并 p2 -> p1 -> 一键撤回拆分恢复 p2
    merge_res = await execute_personal_action(
        platform,
        turn,
        {"action": "merge_people", "source_id": p2["id"], "target_id": p1["id"]},
        now=1_700_000_001.0,
    )
    assert merge_res["ok"] is True
    merged_src = (await platform.people.get_person(alice, p2["id"]))["person"]
    assert merged_src["merged_into_id"] == p1["id"]

    unmerge_res = await undo_executed_action(platform, alice, turn.executed_actions[-1])
    assert unmerge_res["undone"] == 1
    assert (await platform.people.get_person(alice, p2["id"]))["person"]["merged_into_id"] is None


async def test_relay_group_publishes_step_and_summary_artifacts(
    butler: dict[str, Any], step: Any,
) -> None:
    """接力编组每步验收通过后自动冻结 Trace Artifact 传入下一步，并在终态产出编组汇总产物。"""
    world = butler["world"]
    platform = world.platform

    plan = {
        "title": "调研并整理工单",
        "steps": [
            {
                "mode": "relay",
                "members": [
                    {"package": "support-desk", "entry": "triage", "brief": "第一步核查工单"},
                    {"package": "support-desk", "entry": "triage", "brief": "第二步汇总结论"},
                ],
            }
        ],
    }
    world.models.script(
        step(calls=[("g1", "dispatch_group", {"plan": plan})]),
        step(text="已安排接力处理。"),
        step(text="工单 #101 存在退款延迟风险。"),
        step(text="综合结论：优先处理工单 #101。"),
    )
    resp = await butler["http"].post(
        "/v1/assistant/messages",
        json={"request_id": "relay_art_1", "text": "先核查再汇总工单"},
    )
    assert resp.status_code == 200, resp.text
    trace_id = resp.json()["trace_id"]
    group_id = resp.json()["groups"][0]

    owner = TraceOwner(tenant_id="acme", user_id="usr_alice")
    import asyncio

    async with asyncio.timeout(15):
        while True:
            await platform.butler.wakeup.run_once()
            grp = await platform.butler.groups.get(owner, group_id)
            if grp.summarized:
                break
            await asyncio.sleep(0.02)

    artifacts = await platform.trace_artifacts.list_for_trace(butler["alice"], trace_id)
    names = [a.name for a in artifacts]
    assert any(n.startswith("step_1_m1_") for n in names), names
    assert any(n.startswith("step_1_m2_") for n in names), names
    assert any(n.startswith("group_summary_") for n in names), names

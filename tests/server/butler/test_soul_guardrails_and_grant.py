"""管家 SOUL 静态注入、并发上限、plan 流分离、唤醒轮护栏、日程冲突、可撤回纠错与试运行 Grant 收敛测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from server.core.errors import InvalidRequestError
from server.engine.butler.config import ButlerConfig
from server.engine.butler.grant_converger import ConvergedGrantProposal, converge_grant_from_trace
from server.engine.butler.models import ArtisanRef, DispatchRecord
from server.engine.butler.personal_actions import execute_personal_action
from server.engine.butler.turn import ButlerTurn, WakeContext
from server.engine.tracing.models import (
    AttemptUsage,
    SpanRecord,
    TraceOwner,
    TraceRecord,
    TraceView,
)


async def test_soul_static_injection_and_plan_stream_separation(
    butler: dict[str, Any], step: Any,
) -> None:
    """SOUL.md 静态注入进系统提示且工具前预告走独立 event: plan，不污染最终落盘正文。"""
    world = butler["world"]
    cfg = ButlerConfig()
    assert 4 <= cfg.max_parallel_tool_calls <= 16

    world.models.script(
        step(text="我先帮你查一下今天的待办清单。", calls=[("c1", "read_personal", {"kind": "todos"})]),
        step(text="今日暂无未完成待办。"),
    )
    resp = await butler["http"].post(
        "/v1/assistant/messages",
        headers={"Accept": "text/event-stream"},
        json={"request_id": "soul_plan_1", "text": "看看今天有什么待办"},
    )
    assert resp.status_code == 200, resp.text
    raw_sse = resp.text

    # 1. 验证 SOUL.md 已通过静态 InstructionLayer 注入进发往模型的系统提示词
    first_req = world.models.requests[0]
    prompt_text = "\n".join(str(p) for p in first_req.system_prompt)
    assert "玄圃管家素养准则" in prompt_text
    assert "扛住复杂度，只留清净" in prompt_text

    # 2. 验证 SSE 包含独立 event: plan 且最终 receipt 回复仅含末轮正文（不含开场预告）
    assert "event: plan\n" in raw_sse
    assert "我先帮你查一下今天的待办清单。" in raw_sse
    receipt_lines = [
        line.removeprefix("data: ")
        for block in raw_sse.split("\n\n")
        if "event: receipt" in block
        for line in block.splitlines()
        if line.startswith("data: ")
    ]
    assert len(receipt_lines) == 1
    receipt_payload = json.loads(receipt_lines[0])
    assert receipt_payload["message"] == "今日暂无未完成待办。"


async def test_wake_turn_personal_action_whitelist_blocks_destructive_ops(
    butler: dict[str, Any],
) -> None:
    """被唤醒复核轮下，act_personal 仅放行纯追加动作，拦截完成/删除/局部覆盖防二阶提示注入。"""
    platform = butler["world"].platform
    alice = butler["alice"]
    now = datetime.now(UTC)
    todo = await platform.workspace.create(
        alice, "todo", {"title": "核对合同", "status": "open"}, "seed_todo_wake"
    )
    dispatch = DispatchRecord(
        dispatch_id="dsp_wake_1",
        tenant_id="acme",
        user_id="usr_alice",
        request_id="req_dsp_wake_1",
        input_fingerprint="a" * 64,
        trace_id="trc_wake_guard",
        parent_span_id="sp_wake_root",
        artisan=ArtisanRef(
            package="crm-helper",
            version="1.0.0",
            entry="brief",
            digest="sha256:0001",
            decision_ref="dec_wake_1",
        ),
        instruction="整理客户资料",
        host="session",
        state="completed",
        created_at=now,
        updated_at=now,
    )
    wake_turn = ButlerTurn(
        who=alice,
        trace_id="trc_wake_guard",
        request_id="req_wake_guard",
        timezone="Asia/Shanghai",
        wake=WakeContext(dispatch=dispatch, artisan_text="已完成客户资料核查", can_answer=False, passed=True),
    )

    # 破坏性/覆盖性动作在唤醒轮被硬拦截
    for forbidden in (
        {"action": "complete_todo", "item_id": todo["id"]},
        {"action": "delete_todo", "item_id": todo["id"]},
        {"action": "cancel_event", "item_id": todo["id"]},
        {"action": "patch_personal_file", "item_id": todo["id"], "edits": [{"target": "a", "replacement": "b"}]},
    ):
        with pytest.raises(InvalidRequestError, match="禁止执行覆盖或删除类私人操作"):
            await execute_personal_action(platform, wake_turn, forbidden, now=1_700_000_000.0)

    # 安全追加类动作（save_note_file）在唤醒轮允许执行
    saved = await execute_personal_action(
        platform,
        wake_turn,
        {"action": "save_note_file", "name": "复核摘录.md", "content": "# 摘录\n\n验收通过"},
        now=1_700_000_000.0,
    )
    assert saved["ok"] is True


async def test_event_conflict_adjacent_detection_and_reversible_corrections(
    butler: dict[str, Any], step: Any,
) -> None:
    """创建/更新日程确定性返回冲突与 60 分钟内紧邻缓冲；cancel_event / delete_todo 支持一键撤回。"""
    platform = butler["world"].platform
    alice = butler["alice"]
    base = 1_764_144_000.0  # 固定基准时间戳

    # 先安排 10:00–11:00 的早会
    ev1 = await platform.workspace.create(
        alice,
        "event",
        {"title": "产品周会", "start_at": base, "end_at": base + 3600, "timezone": "Asia/Shanghai"},
        "seed_ev_1",
    )
    turn = ButlerTurn(
        who=alice, trace_id="trc_sched", request_id="req_sched_1", timezone="Asia/Shanghai",
    )

    # 1. 创建重叠日程（10:30–11:30）-> 命中 conflicts
    res_conflict = await execute_personal_action(
        platform,
        turn,
        {
            "action": "create_event",
            "title": "客户沟通",
            "start_at": base + 1800,
            "end_at": base + 5400,
            "timezone": "Asia/Shanghai",
        },
        now=base,
    )
    assert len(res_conflict["conflicts"]) == 1
    assert res_conflict["conflicts"][0]["id"] == ev1["id"]

    # 2. 更新为 11:30–12:30（与 10:00–11:00 间隔 30 分钟 <= 60 分钟）-> 无冲突但命中 adjacent_events
    ev2_id = res_conflict["item"]["id"]
    res_adj = await execute_personal_action(
        platform,
        turn,
        {
            "action": "update_event",
            "item_id": ev2_id,
            "start_at": base + 5400,
            "end_at": base + 9000,
        },
        now=base,
    )
    assert res_adj["conflicts"] == []
    assert len(res_adj["adjacent_events"]) == 1
    assert res_adj["adjacent_events"][0]["relation"] == "before"
    assert res_adj["adjacent_events"][0]["gap_minutes"] == 30.0

    # 3. 通过管家对话执行 cancel_event 与 delete_todo，并验证 /undo 完整恢复
    todo = await platform.workspace.create(
        alice, "todo", {"title": "错误待办", "status": "open"}, "seed_todo_del"
    )
    butler["world"].models.script(
        step(calls=[
            ("c1", "act_personal", {"action": "cancel_event", "item_id": ev2_id}),
            ("c2", "act_personal", {"action": "delete_todo", "item_id": todo["id"]}),
        ]),
        step(text="已帮你取消该日程并移出错误待办。"),
    )
    resp = await butler["http"].post(
        "/v1/assistant/messages",
        json={"request_id": "corr_1", "text": "取消刚才的客户沟通日程并删掉那条错误待办"},
    )
    assert resp.status_code == 200, resp.text
    actions = resp.json()["executed_actions"]
    assert len(actions) == 2

    assert (await platform.workspace.get(alice, ev2_id))["trashed_at"] is not None
    assert (await platform.workspace.get(alice, todo["id"]))["trashed_at"] is not None

    # 逐项一键撤回恢复
    for act in actions:
        undo_resp = await butler["http"].post(f"/v1/assistant/actions/corr_1/{act['action_id']}/undo")
        assert undo_resp.status_code == 200, undo_resp.text
        assert undo_resp.json()["result"]["undone"] == 1

    assert (await platform.workspace.get(alice, ev2_id))["trashed_at"] is None
    assert (await platform.workspace.get(alice, todo["id"]))["trashed_at"] is None


def test_converge_grant_from_trace_and_wildcard_rejection() -> None:
    """试运行 Trace 自动收敛最小权限 Grant（实测 Token × 1.5），且严禁任何通配符。"""
    now = datetime.now(UTC)
    trace = TraceView(
        trace=TraceRecord(
            trace_id="trc_trial_1",
            owner=TraceOwner(tenant_id="acme", user_id="usr_alice"),
            origin="user",
            title="试运行客户晨报",
            created_at=now,
            updated_at=now,
        ),
        spans=(
            SpanRecord(
                span_id="sp_root",
                parent_span_id=None,
                kind="butler",
                name="butler",
                state="completed",
                started_at=now,
                ended_at=now,
                attempts=(
                    AttemptUsage(
                        attempt_id="att_1",
                        input_tokens=12_000,
                        output_tokens=4_000,
                        usage_complete=True,
                    ),
                ),
            ),
            SpanRecord(
                span_id="sp_t1",
                parent_span_id="sp_root",
                kind="tool",
                name="read_personal",
                state="completed",
                started_at=now,
                ended_at=now,
            ),
            SpanRecord(
                span_id="sp_t2",
                parent_span_id="sp_root",
                kind="tool",
                name="crm.search_accounts",
                state="completed",
                started_at=now,
                ended_at=now,
            ),
        ),
    )
    dispatch = DispatchRecord(
        dispatch_id="dsp_trial_1",
        tenant_id="acme",
        user_id="usr_alice",
        request_id="req_dsp_trial_1",
        input_fingerprint="b" * 64,
        trace_id="trc_trial_1",
        parent_span_id="sp_root",
        artisan=ArtisanRef(
            package="crm-helper",
            version="1.2.0",
            entry="brief",
            digest="sha256:0002",
            decision_ref="dec_trial_1",
        ),
        instruction="查客户概况",
        host="session",
        state="completed",
        created_at=now,
        updated_at=now,
    )

    proposal = converge_grant_from_trace(trace, [dispatch])
    assert proposal.artisan_refs == ("crm-helper@1.2.0:brief",)
    assert proposal.allowed_tools == ("crm.search_accounts", "read_personal")
    assert proposal.observed_tokens == 16_000
    assert proposal.recommended_max_tokens == 24_000  # 16_000 * 1.5
    assert proposal.require_approval_on_unlisted is True

    # 反通配符铁律
    with pytest.raises(ValueError, match="禁止通配符"):
        ConvergedGrantProposal(
            source_trace_id="trc_trial_1",
            allowed_tools=("crm.*",),
            observed_tokens=100,
            recommended_max_tokens=10_000,
        )

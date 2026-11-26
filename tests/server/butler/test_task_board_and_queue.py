"""跨轮持久任务板（DAG + Working Context）与同会话 FIFO/三阶段强制插入合并测试（ADR 0024）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from server.core.errors import ConflictError, NotFoundError
from server.engine.butler.task_board import (
    MemoryTaskBoardStore,
    TaskBoardRecord,
    TaskBoardStep,
    ready_steps,
)
from server.engine.tracing.models import TraceOwner

from .conftest import employee, enable

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")
BOB = TraceOwner(tenant_id="acme", user_id="usr_bob")


def test_task_board_dag_validation_and_ready_steps() -> None:
    """DAG 校验拒绝重名、未知依赖与环；ready_steps 仅返回前序均完成的 pending 步骤。"""
    now = datetime(2026, 11, 27, tzinfo=UTC)
    board = TaskBoardRecord(
        board_id="btb_1", tenant_id="acme", user_id="usr_alice", trace_id="trc_1",
        title="整理复盘", focus="核对数据",
        steps=(
            TaskBoardStep(id="s1", title="拉取订单", status="done"),
            TaskBoardStep(id="s2", title="财务核对", status="pending", blocked_by=("s1",)),
            TaskBoardStep(id="s3", title="出简报", status="pending", blocked_by=("s2",)),
        ),
        created_at=now, updated_at=now,
    )
    assert [s.id for s in ready_steps(board)] == ["s2"]

    # 环检测
    with pytest.raises(ValueError, match="循环"):
        TaskBoardRecord(
            board_id="btb_cycle", tenant_id="acme", user_id="usr_alice", trace_id="trc_2",
            title="有环",
            steps=(
                TaskBoardStep(id="a", title="A", blocked_by=("b",)),
                TaskBoardStep(id="b", title="B", blocked_by=("a",)),
            ),
            created_at=now, updated_at=now,
        )

    # 未知依赖
    with pytest.raises(ValueError, match="不存在的依赖"):
        TaskBoardRecord(
            board_id="btb_bad", tenant_id="acme", user_id="usr_alice", trace_id="trc_3",
            title="未知依赖",
            steps=(TaskBoardStep(id="a", title="A", blocked_by=("missing",)),),
            created_at=now, updated_at=now,
        )


async def test_task_board_store_isolation_and_cas() -> None:
    """任务板按员工强隔离，支持同 trace 幂等更新与版本 CAS。"""
    store = MemoryTaskBoardStore()
    now = datetime(2026, 11, 27, tzinfo=UTC)
    rec = TaskBoardRecord(
        board_id="btb_1", tenant_id="acme", user_id="usr_alice", trace_id="trc_1",
        title="拜访准备", focus="查背景", created_at=now, updated_at=now,
    )
    saved = await store.upsert(ALICE, rec)
    assert saved.version == 1

    with pytest.raises(NotFoundError):
        await store.get(BOB, saved.board_id)

    updated = await store.upsert(
        ALICE, saved.model_copy(update={"focus": "已出简报", "status": "completed"}),
        expected_version=1,
    )
    assert updated.version == 2
    assert await store.list_active(ALICE) == ()
    assert len(await store.list_recent(ALICE)) == 1

    with pytest.raises(ConflictError):
        await store.upsert(ALICE, saved, expected_version=1)


async def test_butler_tool_updates_task_board_and_injects_working_context(
    butler: dict[str, Any], step: Any,
) -> None:
    """管家通过 update_task_board 持久化任务板，下轮输入 JSON 自动带入 working_context 且 HTTP 可查。"""
    world = butler["world"]
    world.models.script(
        step(calls=[("c1", "update_task_board", {
            "title": "筹备 Q4 评审",
            "focus": "等待财务数据",
            "status": "active",
            "steps": [
                {"id": "s1", "title": "收集销售表", "status": "done"},
                {"id": "s2", "title": "核对折扣", "status": "in_progress", "blocked_by": ["s1"]},
            ],
        })]),
        step(text="已记入任务板并持续跟进。"),
        step(text="收到，继续跟进 Q4 评审。"),
    )
    r1 = await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "tb1", "text": "筹备 Q4 评审"})
    assert r1.status_code == 200, r1.text

    # HTTP 只读接口本人可见、他人不可见
    listed = (await butler["http"].get("/v1/assistant/task-boards?active_only=true")).json()["items"]
    assert len(listed) == 1
    assert listed[0]["title"] == "筹备 Q4 评审"
    assert listed[0]["focus"] == "等待财务数据"

    butler["as"]["who"] = await employee(world, "usr_bob")
    bob_listed = (await butler["http"].get("/v1/assistant/task-boards")).json()["items"]
    assert bob_listed == []

    # 切回 Alice，验证下一轮输入 payload 包含 working_context.task_boards
    butler["as"]["who"] = butler["alice"]
    r2 = await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "tb2", "text": "进展如何"})
    assert r2.status_code == 200, r2.text
    last_req = world.models.requests[-1]
    assert "筹备 Q4 评审" in str(last_req.messages[-1]) and "working_context" in str(last_req.messages[-1])


async def test_same_session_fifo_queue_when_serialized(
    butler: dict[str, Any], step: Any,
) -> None:
    """开启 serialize_user_turns 时，前轮进行中发来新消息先落盘并返回 queued，前轮结束后自动续跑。"""
    enable(butler["world"], reply_window_seconds=1.0, serialize_user_turns=True)
    world = butler["world"]
    gate = asyncio.Event()
    world.models.script_for("第一件事", step(text="第一件事办完了。", wait=gate))
    world.models.script_for("第二件事", step(text="第二件事也办完了。"))

    first = (await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "q1", "text": "第一件事"})).json()
    assert first["state"] == "running"

    second = (await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "q2", "text": "第二件事"})).json()
    assert second["state"] == "queued" and second["queue_position"] == 1

    gate.set()
    async with asyncio.timeout(8.0):
        while True:
            msgs = await world.platform.workspace.messages(butler["alice"], limit=50, latest=True)
            texts = [m["text"] for m in msgs if m["role"] == "assistant"]
            if "第一件事办完了。" in texts and "第二件事也办完了。" in texts:
                break
            await asyncio.sleep(0.03)
    # 两条原话均完整保留
    user_texts = [m["text"] for m in msgs if m["role"] == "user"]
    assert "第一件事" in user_texts and "第二件事" in user_texts


async def test_interrupt_merge_stage1_cancels_and_merges_inputs(
    butler: dict[str, Any], step: Any,
) -> None:
    """阶段 1 强制插入合并：前轮尚无写副作用时直接取消，合并两条原文后由新轮次一次性办妥。"""
    enable(butler["world"], reply_window_seconds=1.0, serialize_user_turns=True)
    world = butler["world"]
    gate = asyncio.Event()
    world.models.script_for("帮我约王总", step(text="（不应送出）", wait=gate))
    world.models.script_for("改到周五下午", step(text="明白，已合并为你约王总周五下午见面。"))

    first = (await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "m1", "text": "帮我约王总"})).json()
    assert first["state"] == "running"

    merged = (await butler["http"].post(
        "/v1/assistant/messages",
        json={"request_id": "m2", "text": "改到周五下午", "mode": "interrupt_merge"},
    )).json()
    assert merged["state"] == "done"
    assert merged["merged_request_ids"] == ["m1", "m2"]
    assert "周五下午" in merged["message"]


async def test_interrupt_merge_stage2_and_stage3_classification(butler: dict[str, Any]) -> None:
    """阶段 2 存在可撤回私域写入时倒序回滚；阶段 3 存在在途派工时保护性合并并透传在途摘要。"""
    from server.engine.butler.models import ArtisanRef, DispatchRecord
    from server.engine.butler.session_queue import ActiveSessionSlot, classify_stage
    from server.engine.butler.turn import ButlerTurn

    platform = butler["world"].platform
    alice = butler["alice"]
    now = datetime(2026, 11, 27, tzinfo=UTC)

    # 先创建一条真实待办模拟阶段 2 的已执行动作
    todo = await platform.workspace.create(
        alice, "todo", {"title": "旧待办", "status": "open"}, "req_todo_stage2")
    t2 = ButlerTurn(
        who=alice, request_id="s2_1", trace_id="trc_s2", timezone="Asia/Shanghai",
        executed_actions=[{
            "action_id": "a1", "action": "create_todo", "undoable": True,
            "undo": {"kind": "workspace_trash", "item_id": todo["id"]},
        }],
    )
    assert classify_stage(t2) == "undo_and_merge"

    gate = asyncio.Event()

    async def _hang() -> dict[str, Any]:
        await gate.wait()
        return {}

    superseded: list[tuple[str, tuple[str, ...]]] = []
    task2 = asyncio.create_task(_hang())
    platform.butler.session_queue.register_active(ActiveSessionSlot(
        user_key="acme:usr_alice", request_id="s2_1", text="建个待办", who=alice,
        task=task2, on_superseded=lambda nid, ids: superseded.append((nid, ids)), turn=t2,
    ))
    merged2 = await platform.butler.session_queue.interrupt_and_prepare_merge(
        "acme:usr_alice", "s2_2", "改成明天的待办")
    assert merged2 is not None and merged2.stage == "undo_and_merge"
    assert superseded == [("s2_2", ("s2_1", "s2_2"))]
    # 原待办已被倒序撤回（回收）
    active_todos = await platform.workspace.list(alice, kind="todo")
    assert all(item["id"] != todo["id"] for item in active_todos)

    # 阶段 3：已有在途派工，保护性合并且保留派工摘要
    dsp = DispatchRecord(
        dispatch_id="dsp_s3", tenant_id="acme", user_id="usr_alice",
        request_id="s3_1:d1", input_fingerprint="a" * 64, trace_id="trc_s3",
        artisan=ArtisanRef(package="support-desk", version="1.0.0", digest="b" * 64,
                           entry="triage", decision_ref="catalog:support-desk@1.0.0"),
        host="session", instruction="查工单", state="running",
        created_at=now, updated_at=now,
    )
    t3 = ButlerTurn(
        who=alice, request_id="s3_1", trace_id="trc_s3", timezone="Asia/Shanghai",
        dispatches=[dsp],
    )
    assert classify_stage(t3) == "protective_merge"
    task3 = asyncio.create_task(_hang())
    platform.butler.session_queue.register_active(ActiveSessionSlot(
        user_key="acme:usr_alice", request_id="s3_1", text="查工单", who=alice,
        task=task3, on_superseded=lambda _n, _i: None, turn=t3,
    ))
    merged3 = await platform.butler.session_queue.interrupt_and_prepare_merge(
        "acme:usr_alice", "s3_2", "顺便查客户电话")
    assert merged3 is not None and merged3.stage == "protective_merge"
    assert merged3.prior_in_flight is not None
    assert merged3.prior_in_flight["dispatches"][0]["dispatch_id"] == "dsp_s3"


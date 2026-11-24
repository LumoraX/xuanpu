"""编组的进程故障点：真实子进程推进编组，在指定步骤停住等待 SIGKILL。

只接合成测试库的独占命名空间；不启动模型（不复核、不比对）、不接外部连接，也不派工
（派工器替身一旦被调用就报错，证明故障点不会多派）。阶段：
- ``after_member_result``：唤醒处理了一位已完成的成员，成员结果已登记进编组，事件尚未确认；
- ``before_ruling``：会诊比对结论已保存，裁决项与裁决消息已写，编组尚未记下裁决项标识。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
from taifeng_stores import SqlNamespace
from server.engine.butler.config import ButlerConfig
from server.engine.butler.groups.runner import GroupRunner
from server.engine.butler.groups.trace import TracedGroupStore
from server.engine.butler.pending import PendingService
from server.engine.butler.trace_link import TracedDispatchStore
from server.engine.butler.wake_apply import WakeEffects
from server.engine.butler.wakeup import Wakeup
from server.core.controlplane.postgres import PostgresControlPlane
from server.domains.identity.postgres import PostgresIdentityStore
from server.core.objectstore import LocalObjectStore
from server.engine.tracing.config import ExecutionTraceConfig
from server.engine.tracing.models import TraceOwner
from server.domains.workspace.service import WorkspaceService


async def _no_turns(_tenant: str) -> int:
    """本进程不跑管家轮次，没有可重跑的。"""
    return 0


async def pause(marker: str, phase: str) -> None:
    """写阶段标记后永久等待（等父进程 SIGKILL）。标记只含阶段名。"""
    await asyncio.to_thread(Path(marker).write_text, json.dumps({"phase": phase}))
    await asyncio.Future()


class NoDispatch:
    """派工器替身：故障点之前不应再派工，被调用即失败。"""

    def __getattr__(self, name: str) -> Any:
        """任何调用都报错。"""
        raise RuntimeError("子进程不应派工：" + name)


def _platform(control: PostgresControlPlane, identity: PostgresIdentityStore,
              workspace: WorkspaceService, tenant: str) -> Any:
    """最小平台：只含唤醒与编组推进用到的部件。"""
    trace = ExecutionTraceConfig(enabled=True, tenants=(tenant,))
    config = ButlerConfig(enabled=True, tenants=(tenant,))
    return SimpleNamespace(
        store=control, workspace=workspace, identity=SimpleNamespace(store=identity),
        dispatches=TracedDispatchStore(control.dispatch_store, control.trace_store, lambda: trace),
        butler_config=config,
        # 只验编组推进：不跑管家轮次，中断轮次的重跑（R6-D）在这里无事可做
        butler=SimpleNamespace(enabled=lambda t: t == tenant, resume_stale=_no_turns),
        runtimes=SimpleNamespace(defaults=SimpleNamespace(execution_trace=trace)))


def _patch_save(groups: TracedGroupStore, phase: str, marker: str) -> None:
    """在故障点停住：成员结果保存之后，或带裁决项标识的保存之前。"""
    save = groups.save

    async def paused(owner: Any, record: Any, *, expected_version: int) -> Any:
        """按阶段决定在保存前还是保存后停住。"""
        verdict = record.verdict(0)
        if phase == "before_ruling" and verdict is not None and verdict.pending_id is not None:
            await pause(marker, phase)
        result = await save(owner, record, expected_version=expected_version)
        if phase == "after_member_result" and any(run.passed is not None for run in record.runs):
            await pause(marker, phase)
        return result

    groups.save = paused  # type: ignore[method-assign]


async def run(config: dict[str, Any]) -> None:
    """装配最小依赖，按阶段推进一次。"""
    pool = await asyncpg.create_pool(config["dsn"], min_size=1, max_size=4)
    namespace = SqlNamespace(schema=config["schema"], prefix=config["prefix"])
    control = PostgresControlPlane(pool, namespace=namespace)
    identity = PostgresIdentityStore(pool, namespace=namespace)
    workspace = WorkspaceService(store=control.workspace_store, control=control, identity=identity,
                                 objects=LocalObjectStore(Path(config["objects_dir"])))
    platform = _platform(control, identity, workspace, config["tenant"])
    groups = TracedGroupStore(control.group_store, control.trace_store,
                              lambda: platform.runtimes.defaults.execution_trace)
    _patch_save(groups, config["phase"], config["marker"])
    runner = GroupRunner(platform, NoDispatch(), groups, now=time.time)  # type: ignore[arg-type]
    pending = PendingService(platform, now=time.time)
    pending.bind_ruling(runner.rule)
    try:
        if config["phase"] == "before_ruling":
            owner = TraceOwner(tenant_id=config["tenant"], user_id=config["user"])
            await runner.advance(owner, config["group_id"])
        else:
            effects = WakeEffects(platform, pending, now=time.time, groups=runner)
            await Wakeup(platform, effects, None, now=time.time, groups=runner).run_once()
    finally:
        await pool.close()


def main() -> None:
    """读取权限 600 的私有配置文件。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    path = Path(parser.parse_args().config)
    asyncio.run(run(json.loads(path.read_text())))


if __name__ == "__main__":
    main()

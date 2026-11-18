"""管家唤醒的进程故障点：真实子进程处理一条唤醒事件，在指定步骤后停住等待 SIGKILL。

只接合成测试库的独占命名空间；不启动模型（不复核）、不接外部连接。阶段：
- ``after_decide``：决定已持久化、尚未写交付消息；
- ``after_message``：交付消息已写进主对话、派工尚未标为 delivered、事件尚未确认。
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
from xuanpu_server.butler.config import ButlerConfig
from xuanpu_server.butler.pending import PendingService
from xuanpu_server.butler.wake_apply import WakeEffects
from xuanpu_server.butler.wakeup import Wakeup
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.identity.postgres import PostgresIdentityStore
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.workspace.service import WorkspaceService


async def _no_turns(_tenant: str) -> int:
    """本进程不跑管家轮次，没有可重跑的。"""
    return 0


async def pause(marker: str, phase: str) -> None:
    """写阶段标记后永久等待（等父进程 SIGKILL）。标记只含阶段名。"""
    await asyncio.to_thread(Path(marker).write_text, json.dumps({"phase": phase}))
    await asyncio.Future()


async def run(config: dict[str, Any]) -> None:
    """装配最小依赖，处理一轮唤醒。"""
    pool = await asyncpg.create_pool(config["dsn"], min_size=1, max_size=4)
    namespace = SqlNamespace(schema=config["schema"], prefix=config["prefix"])
    control = PostgresControlPlane(pool, namespace=namespace)
    identity = PostgresIdentityStore(pool, namespace=namespace)
    workspace = WorkspaceService(store=control.workspace_store, control=control, identity=identity,
                                 objects=LocalObjectStore(Path(config["objects_dir"])))
    phase, marker = config["phase"], config["marker"]
    decide, append = control.wake_store.decide, workspace.append_message

    async def paused_decide(*args: Any, **kwargs: Any) -> Any:
        """决定落库后停住。"""
        result = await decide(*args, **kwargs)
        if phase == "after_decide":
            await pause(marker, phase)
        return result

    async def paused_append(*args: Any, **kwargs: Any) -> Any:
        """交付消息落库后停住。"""
        result = await append(*args, **kwargs)
        if phase == "after_message" and str(args[3]).endswith(":deliver"):
            await pause(marker, phase)
        return result

    control.wake_store.decide = paused_decide  # type: ignore[method-assign]
    workspace.append_message = paused_append  # type: ignore[method-assign]
    tenants = (config["tenant"],)
    platform: Any = SimpleNamespace(
        store=control, dispatches=control.dispatch_store, workspace=workspace,
        identity=SimpleNamespace(store=identity),
        butler_config=ButlerConfig(enabled=True, tenants=tenants),
        # 只验唤醒交付：不跑管家轮次，中断轮次的重跑（R6-D）在这里无事可做
        butler=SimpleNamespace(enabled=lambda tenant: tenant in tenants, resume_stale=_no_turns))
    pending = PendingService(platform, now=time.time)
    wakeup = Wakeup(platform, WakeEffects(platform, pending, now=time.time), None,
                    now=time.time)
    try:
        await wakeup.run_once()
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

"""隔离进程验证工作台后端可独立导入。"""

from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "workspace.memory",
        "workspace.postgres",
        "workspace.service",
        "controlplane.memory",
        "controlplane.postgres",
    ],
)
async def test_standalone_workspace_import(module):
    """不依赖测试收集顺序预先导入控制面。"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        f"import xuanpu_server.{module}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 0, error.decode()

"""独立进程入口避免pytest导入顺序掩盖循环依赖。"""

from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.mark.parametrize(
    "code",
    [
        "import xuanpu_server.config; import xuanpu_server.bootstrap",
        "import xuanpu_server.identity.memory; import xuanpu_server.bootstrap",
        "from xuanpu_cli.main import main; main()",
    ],
)
async def test_standalone_import_and_cli_entry(code):
    """全新解释器启动配置、身份及CLI帮助入口。"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        "--help",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 0, error.decode()
    if "main()" in code:
        assert b"xuanpu" in output

"""使用同一沙盒后端组装文件与命令工具，禁止宿主文件系统兜底。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import taifeng

from xuanpu_server.errors import RuntimeUnavailableError

if TYPE_CHECKING:
    from xuanpu_server.runtime.sandbox import SandboxHandle


def office_tools(handle: SandboxHandle, policy: taifeng.PermissionPolicy) -> list[taifeng.ToolSpec]:
    """所有读写和进程都由同一沙盒执行，环境只含显式最小集合。"""
    if handle.filesystem is None:
        raise RuntimeUnavailableError('办公执行后端未提供文件协议')
    return [
        taifeng.make_file_read_tool(workspace=handle.filesystem, policy=policy),
        taifeng.make_file_write_tool(workspace=handle.filesystem, policy=policy),
        taifeng.make_shell_exec_tool(policy=policy, cwd=handle.workspace_inside,
            env=handle.process_env(), executor=handle.executor,
            timeout_seconds=60, max_output_bytes=65536),
    ]

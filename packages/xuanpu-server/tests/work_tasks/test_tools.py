"""办公文件和命令使用同一个实际沙盒，输入挂载不能写。"""

from __future__ import annotations

import pytest
import taifeng
from xuanpu_server.runtime.approval import build_policy
from xuanpu_server.work_tasks.tools import office_tools


def context():
    """构造真实工具上下文。"""
    return taifeng.ToolContext(call_id='c1', thread_id='thread', cancel=taifeng.CancellationToken())


@pytest.mark.parametrize('world', ['local', 'docker'], indirect=True)
async def test_commands_and_file_tools_share_workspace(world):
    """同一执行器写出的内容能被同后端文件工具读取，越界写入拒绝。"""
    await world.publish()
    package = await world.platform.packages.resolve('acme', 'support-desk')
    runtime = await world.platform.runtimes.get(package)
    handle = runtime.sandbox
    policy = build_policy('relaxed', workspace_root=handle.workspace_inside)
    tools = {tool.name: tool for tool in office_tools(handle, policy)}
    result = await tools['shell_exec'].handler({'command': 'mkdir -p outputs; printf result > outputs/result.txt'}, context())
    assert not result.is_error
    result = await tools['file_read'].handler({'path': 'outputs/result.txt'}, context())
    assert 'result' in str(result)
    denied = await tools['file_write'].handler({'path': 'inputs/overwrite.txt', 'content': 'bad'}, context())
    assert denied.is_error
    with pytest.raises(taifeng.SuspendSignal):
        await build_policy('relaxed', {'erp.save': 'write'}, contains_external_input=True).check(
            taifeng.PermissionRequest(scope='tool_use', target='erp.save', reason='test'))


@pytest.mark.parametrize('world', ['docker'], indirect=True)
async def test_input_mount_is_readonly_for_commands_and_files(world, tmp_path):
    """真实容器内root进程也无法覆盖只读来源。"""
    await world.publish()
    package = await world.platform.packages.resolve('acme', 'support-desk')
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    (inputs / 'input_1.txt').write_text('immutable')
    handle = await world.platform.runtimes._sandboxes.open(package, tmp_path / 'work', input_root=inputs)
    try:
        process = await handle.executor.start(taifeng.CommandSpec(command="printf bad > inputs/input_1.txt",
            shell=True, cwd=handle.workspace_inside, env=handle.process_env()))
        assert await process.wait() != 0
        with pytest.raises(OSError):
            await handle.filesystem.write_bytes('inputs/input_1.txt', b'bad')
        assert await handle.filesystem.read_bytes('inputs/input_1.txt') == b'immutable'
        assert (inputs / 'input_1.txt').read_text() == 'immutable'
    finally:
        await handle.close()


@pytest.mark.parametrize('world', ['docker'], indirect=True)
async def test_actual_shell_timeout_reaps_the_process(world):
    """缩短同一公共工具的时限，实际超时后不能留下正在运行的命令。"""
    await world.publish()
    package = await world.platform.packages.resolve('acme','support-desk')
    runtime = await world.platform.runtimes.get(package)
    handle = runtime.sandbox
    tool = taifeng.make_shell_exec_tool(policy=build_policy('relaxed',workspace_root=handle.workspace_inside),
        cwd=handle.workspace_inside,env=handle.process_env(),executor=handle.executor,
        timeout_seconds=0.2,max_output_bytes=1024)
    result = await tool.handler({'command':'mkdir -p scratch; echo $$ > scratch/timed-pid; sleep 30'},context())
    assert result.is_error
    probe = await handle.executor.start(taifeng.CommandSpec(command='kill -0 "$(cat scratch/timed-pid)" 2>/dev/null',
        shell=True,cwd=handle.workspace_inside,env=handle.process_env()))
    await probe.communicate()
    assert await probe.wait() != 0

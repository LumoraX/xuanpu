"""在真实服务器容器中验证 bubblewrap 的写入、秘密与网络隔离。"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from taifeng import CommandSpec
from taifeng_sandbox import SandboxPolicy
from taifeng_sandbox.local import create_local_executor


async def main() -> None:
    """使用部署配置运行真实脚本，失败直接退出而不降级执行。"""
    if not Path('/run/xuanpu/config.json').is_file():
        raise RuntimeError('探针需要平台 JSON 的真实只读挂载')
    with tempfile.TemporaryDirectory(prefix='xuanpu-probe-') as temporary:
        workspace = Path(temporary)
        executor = create_local_executor(SandboxPolicy.workspace_only(workspace, network=False))
        command = (
            'python -c "from pathlib import Path; import socket; '
            "assert not Path('/run/xuanpu/config.json').exists(); "
            "assert socket.if_nameindex()==[(1,'lo')]; "
            "assert not Path('/var/lib/xuanpu/objects').exists(); "
            "Path('proof').write_text('isolated'); print('sandbox-isolated')\""
        )
        process = await executor.start(CommandSpec(
            command=command, shell=True, cwd=str(workspace),
            env={'PATH': '/usr/local/bin:/usr/bin:/bin'},
        ))
        output, error = await asyncio.wait_for(process.communicate(), timeout=15)
        if process.returncode != 0:
            raise RuntimeError('真实隔离执行失败：' + error.decode())
        if (workspace / 'proof').read_text() != 'isolated':
            raise RuntimeError('工作区写入未持久化')
        print(output.decode().strip())


if __name__ == '__main__':
    asyncio.run(main())

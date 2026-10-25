"""预检资源的清理责任持续持有，失败目录不能先行删除。"""
from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

from xuanpu_server.errors import RuntimeCleanupRequiredError
from xuanpu_server.runtime.sandbox import LocalSandboxProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from xuanpu_server.runtime.sandbox import SandboxProvider


class ProbeResources:
    """内存保留具体收尾凭证，磁盘目录保留冷恢复的明确资源范围。"""
    def __init__(self, parent: Path, sandboxes: SandboxProvider) -> None:
        """仅回收专属预检根，不触碰普通办公工作区。"""
        self.parent, self.sandboxes = parent, sandboxes
        self.pending: dict[Path, list[Callable[[], Awaitable[None]]]] = {}

    def add(self, root: Path, cleanup: Callable[[], Awaitable[None]]) -> None:
        """成功交接或启动异常返回的清理凭证立即登记。"""
        self.pending.setdefault(root, []).append(cleanup)

    async def cleanup(self, root: Path) -> None:
        """逆序逐项清理，失败继续持有；全部退出后才删除目录。"""
        failed = []
        for close in reversed(self.pending.get(root, [])):
            try:
                await close()
            except BaseException as exc:
                failed.append(exc.cleanup if isinstance(exc, RuntimeCleanupRequiredError) else close)
        if failed:
            self.pending[root] = list(reversed(failed))
            raise RuntimeCleanupRequiredError('预检资源仍待清理，请稍后重试', self.recover)
        await asyncio.to_thread(shutil.rmtree, root)
        self.pending.pop(root, None)

    async def recover(self) -> None:
        """启动和后续预检重试明确预检目录，保留不能核实已退出的现场。"""
        if not await asyncio.to_thread(self.parent.exists):
            return
        roots = await asyncio.to_thread(lambda: sorted(self.parent.iterdir()))
        for root in roots:
            if not root.name.startswith('probe_') or root.is_symlink() or not root.is_dir():
                raise RuntimeCleanupRequiredError('预检目录不符合回收边界', self.recover)
            if root not in self.pending:
                if isinstance(self.sandboxes, LocalSandboxProvider):
                    raise RuntimeCleanupRequiredError('本机遗留探测进程无法核实，请人工核查后清理目录', self.recover)
                async def close_scripts(path: Path = root / 'scripts') -> None:
                    """只回收持久目录绑定的旧脚本容器。"""
                    await self.sandboxes.close_workspace(path)

                async def close_connectors(path: Path = root / 'connectors') -> None:
                    """只回收持久目录绑定的旧连接器容器。"""
                    await self.sandboxes.close_workspace(path)

                self.pending[root] = [close_scripts, close_connectors]
            await self.cleanup(root)

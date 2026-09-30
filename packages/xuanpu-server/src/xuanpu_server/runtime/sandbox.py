"""沙盒供给：为一个运行时准备执行环境。

业务包里的脚本和命令是模型驱动的，必须在隔离环境里运行（ADR 0002、ADR 0004 决策 3）。
业务方只声明需求（要不要出网、要不要命令执行），用哪种隔离后端由部署方在平台配置里选。

隔离范围：一个运行时（租户 + 业务包 + 版本）一个沙盒，其下的会话共用同一个工作区。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from taifeng_sandbox import SandboxPolicy, SandboxUnavailableError
from taifeng_sandbox.docker import DockerEnvironment, DockerSandboxConfig, Mount
from taifeng_sandbox.local import create_local_executor

from xuanpu_server.errors import RuntimeUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    import taifeng

    from xuanpu_server.packages import ResolvedPackage


async def _noop() -> None:
    """什么都不用收尾。"""


@dataclass
class SandboxHandle:
    """一个已就绪的沙盒。

    Attributes:
        executor: 在沙盒里启动进程的执行器。
        workspace: 沙盒里唯一可写的目录（宿主机路径）。
        workspace_inside: 同一个目录在沙盒里看到的路径。
        backend: 隔离后端的名字，写进审计。
        closer: 销毁沙盒。
    """

    executor: taifeng.CommandExecutor
    workspace: Path
    workspace_inside: str
    backend: str
    closer: Callable[[], Awaitable[None]] = field(default=_noop)

    def process_env(self) -> dict[str, str]:
        """沙盒里的进程拿到的环境变量：最小集合，加上指向工作区的几个约定变量。"""
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8",
            "HOME": self.workspace_inside,
            "TMPDIR": self.workspace_inside,
            "WORKSPACE": self.workspace_inside,
        }

    async def close(self) -> None:
        """销毁沙盒。"""
        await self.closer()


@runtime_checkable
class SandboxProvider(Protocol):
    """沙盒供给方。"""

    async def open(self, package: ResolvedPackage, workspace: Path) -> SandboxHandle:
        """为业务包准备沙盒。

        沙盒里的进程只能读业务包目录、只能写 ``workspace``；是否能出网取决于业务包的声明。

        Raises:
            RuntimeUnavailableError: 隔离后端不可用。平台不会退回到无隔离执行。
        """
        ...


class LocalSandboxProvider:
    """本机 OS 级隔离（macOS seatbelt / Linux bubblewrap）。"""

    async def open(self, package: ResolvedPackage, workspace: Path) -> SandboxHandle:
        """用受限读策略建沙盒：平台数据目录里其他租户的文件对沙盒不可见。"""
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        policy = SandboxPolicy.workspace_only(
            workspace,
            readable_roots=(package.root,),
            network=package.manifest.spec.policy.sandbox.network,
        )
        try:
            executor = await asyncio.to_thread(create_local_executor, policy)
        except SandboxUnavailableError as exc:
            raise RuntimeUnavailableError(f"本机隔离后端不可用：{exc}") from exc
        return SandboxHandle(
            executor=executor,
            workspace=workspace,
            workspace_inside=str(workspace),
            backend="local",
        )


class DockerSandboxProvider:
    """容器隔离：每个运行时一个容器。"""

    def __init__(
        self,
        *,
        image: str,
        docker_env: Mapping[str, str],
        memory_mb: int | None = 1024,
        cpus: float | None = 1.0,
        docker_binary: str = "docker",
    ) -> None:
        """
        Args:
            image: 沙盒镜像，须带 Python 3.9+。
            docker_env: 运行 docker 命令时的环境变量。
            memory_mb: 内存上限。
            cpus: CPU 配额。
            docker_binary: docker 命令。
        """
        self._image = image
        self._docker_env = dict(docker_env)
        self._memory_mb = memory_mb
        self._cpus = cpus
        self._docker_binary = docker_binary

    async def open(self, package: ResolvedPackage, workspace: Path) -> SandboxHandle:
        """拉起容器：业务包目录按相同路径只读挂载，工作区可写挂载。"""
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        config = DockerSandboxConfig(
            image=self._image,
            workspace_host_dir=workspace,
            mounts=(Mount(package.root, str(package.root)),),
            network=package.manifest.spec.policy.sandbox.network,
            memory_mb=self._memory_mb,
            cpus=self._cpus,
            docker_binary=self._docker_binary,
            docker_env=self._docker_env,
            labels={
                "xuanpu.tenant": package.record.tenant_id,
                "xuanpu.package": package.record.name,
            },
        )
        try:
            environment = await DockerEnvironment.create(config)
        except SandboxUnavailableError as exc:
            raise RuntimeUnavailableError(f"容器沙盒不可用：{exc}") from exc
        return SandboxHandle(
            executor=environment.executor(),
            workspace=workspace,
            workspace_inside=config.workdir,
            backend="docker",
            closer=environment.close,
        )


__all__ = ["DockerSandboxProvider", "LocalSandboxProvider", "SandboxHandle", "SandboxProvider"]

"""沙盒供给：为一个运行时准备执行环境。

业务包里的脚本和命令是模型驱动的，必须在隔离环境里运行（ADR 0002、ADR 0004 决策 3）。
业务方只声明需求（要不要出网、要不要命令执行），用哪种隔离后端由部署方在平台配置里选。

隔离范围：一个执行scope（租户 + 员工 + 任务或会话）一个脚本沙盒。
业务包声明了 stdio 连接器时，再为连接器进程单独开一个连接器沙盒：同样只读业务包目录，
只写连接器自己的工作目录，但允许出网——连接器就是去连外部系统的；脚本是否出网仍按清单。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import taifeng
from taifeng_sandbox import (
    SandboxOperationOutcomeUnknownError,
    SandboxPolicy,
    SandboxUnavailableError,
)
from taifeng_sandbox.docker import DockerEnvironment, DockerSandboxConfig, Mount
from taifeng_sandbox.local import create_local_executor

from xuanpu_server.errors import RuntimeCleanupRequiredError, RuntimeUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path

    from xuanpu_server.packages import ResolvedPackage

logger = logging.getLogger(__name__)

KILL_WAIT_SECONDS = 5.0
"""强杀进程之后等它退出的时限：关沙盒时的每个进程、收拾起不来的连接器进程都用它。"""

# 沙盒里进程的最小环境：只够找到解释器。宿主自己的环境变量一概不带进去
DEFAULT_BASE_ENV: Mapping[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "C.UTF-8",
}


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
    filesystem: taifeng.WorkspaceFS | None = None
    freezer: Callable[[], Awaitable[None]] | None = None
    frozen: bool = False

    def process_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """沙盒里的进程拿到的环境变量：基础环境，加上指向工作区的几个约定变量。

        Args:
            base: 基础环境；None 时用最小集合 ``DEFAULT_BASE_ENV``。约定变量总是盖过它。
        """
        return {
            **(DEFAULT_BASE_ENV if base is None else base),
            "HOME": self.workspace_inside,
            "TMPDIR": self.workspace_inside,
            "WORKSPACE": self.workspace_inside,
        }

    async def freeze_outputs(self) -> None:
        """停止明确绑定容器的全部进程后才允许宿主安全导出，不做远端兜底。"""
        if self.frozen:
            return
        if self.backend != "docker" or self.freezer is None:
            raise RuntimeUnavailableError("该执行后端尚不支持成果冻结")
        await self.freezer()
        self.frozen = True

    async def close(self) -> None:
        """销毁沙盒。"""
        await self.closer()


@runtime_checkable
class SandboxProvider(Protocol):
    """沙盒供给方。"""

    async def open(
        self, package: ResolvedPackage, workspace: Path, *, input_root: Path | None = None
    ) -> SandboxHandle:
        """为业务包的脚本与命令准备沙盒。

        沙盒里的进程只能读业务包目录、只能写 ``workspace``；是否能出网取决于业务包的声明。

        Raises:
            RuntimeUnavailableError: 隔离后端不可用。平台不会退回到无隔离执行。
        """
        ...

    async def close_workspace(self, workspace: Path) -> None:
        """内部恢复时只回收与该明确工作目录绑定的已知资源。"""
        ...

    async def open_connectors(self, package: ResolvedPackage, workspace: Path) -> SandboxHandle:
        """为业务包的 stdio 连接器准备沙盒，与脚本的沙盒分开。

        沙盒里的进程只读业务包目录（另加部署方给连接器放开的目录）、只写 ``workspace``，
        允许出网。业务包目录在沙盒里与宿主机同路径。

        Raises:
            RuntimeUnavailableError: 隔离后端不可用。平台不会退回到无隔离执行。
        """
        ...


class ProcessTrackingExecutor:
    """包住一个执行器：记下经它起过的全部进程，``close`` 时逐个强杀。

    本机连接器沙盒用它兜底。内核的 ``McpStdioClient.spawn`` 在启动被取消（例如外层的启动
    时限到了）时不会关掉已经起来的进程；本机沙盒又不像容器那样有一个能整个销毁的边界，
    不兜底的话每次组装重试都会漏一个连接器进程。

    强杀按 ``CommandProcess.kill`` 的契约进行：本机后端按进程组杀（连同它派生的进程），已经
    结束的进程上调用不报错。

    Attributes:
        inner: 真正起进程的执行器。
    """

    def __init__(self, inner: taifeng.CommandExecutor) -> None:
        """包住 ``inner``。"""
        self.inner = inner
        self._processes: list[taifeng.CommandProcess] = []
        self._closed = False
        self._starting: set[asyncio.Future[None]] = set()
        self._workers: set[asyncio.Task[taifeng.CommandProcess]] = set()
        self._capture_errors: list[Exception] = []
        self._close_lock = asyncio.Lock()

    def _capture(self, worker: asyncio.Task[taifeng.CommandProcess]) -> None:
        """底层启动一旦返回立即拥有进程，不依赖可能已取消的上层调用者。"""
        self._workers.discard(worker)
        if worker.cancelled() or worker.exception() is not None:
            return
        process = worker.result()
        self._processes.append(process)
        if self._closed:
            try:
                process.kill()
            except Exception as exc:
                self._capture_errors.append(exc)

    @property
    def processes(self) -> tuple[taifeng.CommandProcess, ...]:
        """起过、还没随 ``close`` 收走的进程。"""
        return tuple(self._processes)

    async def start(self, spec: taifeng.CommandSpec) -> taifeng.CommandProcess:
        """经 ``inner`` 起进程并记下。

        Raises:
            OSError: 已经关闭（执行器契约：启动失败抛 ``OSError``）。
        """
        if self._closed:
            raise OSError("沙盒已关闭，不能再起进程")
        finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._starting.add(finished)
        starting = asyncio.create_task(self.inner.start(spec))
        self._workers.add(starting)
        starting.add_done_callback(self._capture)
        try:
            try:
                process = await asyncio.shield(starting)
            except asyncio.CancelledError:
                # 底层创建进程不能因上层取消而失去所有者。
                process = await asyncio.shield(starting)
                process.kill()
                await asyncio.wait_for(process.wait(), KILL_WAIT_SECONDS)
                raise
            if self._closed:
                process.kill()
                await asyncio.wait_for(process.wait(), KILL_WAIT_SECONDS)
                raise OSError("沙盒关闭期间启动的进程已终止")
            return process
        finally:
            self._starting.discard(finished)
            if not finished.done():
                finished.set_result(None)

    async def close(self) -> None:
        """强杀起过的全部进程，并等它们退出。之后不能再起进程。"""
        async with self._close_lock:
            self._closed = True
            errors: list[BaseException] = list(self._capture_errors)
            for process in self._processes:
                try:
                    process.kill()
                except Exception as exc:
                    errors.append(exc)
            waiting: list[Awaitable[object]] = [*self._starting, *self._workers]
            if waiting:
                try:
                    await asyncio.wait_for(asyncio.shield(asyncio.gather(*waiting, return_exceptions=True)), KILL_WAIT_SECONDS)
                except BaseException as exc:
                    errors.append(exc)
            cleanup = asyncio.create_task(self._reap_known())
            while not cleanup.done():
                try:
                    await asyncio.wait({cleanup})
                except asyncio.CancelledError as exc:
                    # 取消只能影响返回结论，不能跳过已启动进程的回收。
                    errors.append(exc)
            errors.extend(cleanup.result())
            if errors:
                if len(errors) == 1:
                    raise errors[0]
                raise BaseExceptionGroup("沙盒进程尚未全部退出", errors)
            self._processes = []

    async def _reap_known(self) -> list[Exception]:
        """启动等待失败后仍终止并等待所有已知进程，错误交回关闭者统一处理。"""
        errors: list[Exception] = []
        for process in self._processes:
            try:
                process.kill()
            except Exception as exc:
                errors.append(exc)
        waits = [asyncio.wait_for(process.wait(), KILL_WAIT_SECONDS) for process in self._processes]
        results = await asyncio.gather(*waits, return_exceptions=True)
        errors.extend(result for result in results if isinstance(result, Exception))
        return errors


class LocalSandboxProvider:
    """本机 OS 级隔离（macOS seatbelt / Linux bubblewrap）。"""

    def __init__(self, *, connector_readable_roots: tuple[Path, ...] = ()) -> None:
        """
        Args:
            connector_readable_roots: 连接器沙盒里额外可读的目录（绝对路径），通常是连接器
                自己的解释器与依赖所在的目录。受限读策略下只有系统运行时目录与业务包目录
                可读，装在别处的解释器不列进来就起不来。
        """
        self._connector_roots = connector_readable_roots

    async def open(
        self, package: ResolvedPackage, workspace: Path, *, input_root: Path | None = None
    ) -> SandboxHandle:
        """用受限读策略建脚本沙盒：平台数据目录里其他租户的文件对沙盒不可见。"""
        if input_root is not None:
            raise RuntimeUnavailableError("本机后端未提供办公输入只读挂载，请使用Docker")
        return await self._open(
            workspace,
            readable_roots=(package.root,),
            network=package.manifest.spec.policy.sandbox.network,
        )

    async def close_workspace(self, workspace: Path) -> None:
        """本机办公只读输入尚未启用，因此不会创建可跨进程遗留的办公资源。"""

    async def open_connectors(self, package: ResolvedPackage, workspace: Path) -> SandboxHandle:
        """用受限读策略建连接器沙盒：可读业务包目录与配置放开的目录，可出网。

        执行器记下起过的进程，沙盒关闭时全部强杀（见 ``ProcessTrackingExecutor``）。
        """
        return await self._open(
            workspace, readable_roots=(package.root, *self._connector_roots), network=True
        )

    @staticmethod
    async def _open(
        workspace: Path, *, readable_roots: tuple[Path, ...], network: bool
    ) -> SandboxHandle:
        """按给定的可读目录与出网开关建一个只写 ``workspace`` 的沙盒。"""
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        policy = SandboxPolicy.workspace_only(
            workspace, readable_roots=readable_roots, network=network
        )
        try:
            executor = await asyncio.to_thread(create_local_executor, policy)
        except SandboxUnavailableError as exc:
            raise RuntimeUnavailableError(f"本机隔离后端不可用：{exc}") from exc
        tracking = ProcessTrackingExecutor(executor)
        return SandboxHandle(
            executor=tracking,
            workspace=workspace,
            workspace_inside=str(workspace),
            backend="local",
            closer=tracking.close,
            filesystem=taifeng.LocalWorkspaceFS(workspace),
        )


def _container_name(workspace: Path) -> str:
    """目录含部署绝对路径及scope摘要，名称在启动前已知且与其他部署隔离。"""
    return 'tfsb-' + hashlib.sha256(str(workspace.absolute()).encode()).hexdigest()[:48]


class DockerSandboxProvider:
    """容器隔离：每个运行时一个脚本容器；有 stdio 连接器时另起一个连接器容器。"""

    def __init__(
        self,
        *,
        image: str,
        docker_env: Mapping[str, str],
        memory_mb: int | None = 1024,
        cpus: float | None = 1.0,
        docker_binary: str = "docker",
        connector_image: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """
        Args:
            image: 沙盒镜像，须带 Python 3.9+。
            docker_env: 运行 docker 命令时的环境变量。
            memory_mb: 内存上限（脚本容器与连接器容器各自的）。
            cpus: CPU 配额（同上）。
            docker_binary: docker 命令。
            connector_image: 连接器容器的镜像，须带 Python 3.9+ 与连接器需要的运行时；
                None 时与 ``image`` 相同。
            labels: 加在每个容器上的额外标签（例如部署标识，便于按部署清理）。与平台自己
                的 ``xuanpu.*`` 标签重名时以平台的为准。
        """
        self._image = image
        self._connector_image = image if connector_image is None else connector_image
        self._docker_env = dict(docker_env)
        self._memory_mb = memory_mb
        self._cpus = cpus
        self._docker_binary = docker_binary
        self._labels = {} if labels is None else dict(labels)

    async def open(
        self, package: ResolvedPackage, workspace: Path, *, input_root: Path | None = None
    ) -> SandboxHandle:
        """拉起脚本容器：业务包目录按相同路径只读挂载，工作区可写挂载。"""
        return await self._launch(
            package,
            workspace,
            image=self._image,
            network=package.manifest.spec.policy.sandbox.network,
            role="scripts",
            input_root=input_root,
        )

    async def close_workspace(self, workspace: Path) -> None:
        """按包含部署路径的确定名称回收，不通过标签扫描无关容器。"""
        config = DockerSandboxConfig(image=self._image, docker_binary=self._docker_binary,
                                     docker_env=self._docker_env)
        await DockerEnvironment.remove(config, _container_name(workspace))

    async def open_connectors(self, package: ResolvedPackage, workspace: Path) -> SandboxHandle:
        """拉起连接器容器：挂载方式与脚本容器相同，镜像用连接器镜像，允许出网。"""
        return await self._launch(
            package,
            workspace,
            image=self._connector_image,
            network=True,
            role="connectors",
        )

    async def _launch(
        self,
        package: ResolvedPackage,
        workspace: Path,
        *,
        image: str,
        network: bool,
        role: str,
        input_root: Path | None = None,
    ) -> SandboxHandle:
        """拉起一个容器：业务包目录按相同路径只读挂载，``workspace`` 可写挂载。

        容器带上租户、业务包名与版本、用途（``scripts`` / ``connectors``）几个标签，便于
        事后按运行时查找与清理。
        """
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        if input_root is not None:
            for directory in ("inputs", "outputs", "scratch"):
                await asyncio.to_thread((workspace / directory).mkdir, exist_ok=True)
        mounts = [Mount(package.root, str(package.root))]
        if input_root is not None:
            mounts.append(Mount(input_root, "/workspace/inputs", read_only=True))
        config = DockerSandboxConfig(
            image=image,
            workspace_host_dir=workspace,
            mounts=tuple(mounts),
            network=network,
            memory_mb=self._memory_mb,
            cpus=self._cpus,
            docker_binary=self._docker_binary,
            docker_env=self._docker_env,
            labels={
                **self._labels,
                "xuanpu.tenant": package.record.tenant_id,
                "xuanpu.package": package.record.name,
                "xuanpu.version": package.record.version,
                "xuanpu.role": role,
            },
        )
        name = _container_name(workspace)

        async def remove_unreturned() -> None:
            """即使daemon未握手，宿主仍拥有这个确定名称的清理责任。"""
            await DockerEnvironment.remove(config, name)

        try:
            environment = await DockerEnvironment.create(config, name=name)
        except BaseException as original:
            try:
                await remove_unreturned()
            except BaseException as cleanup:
                raise RuntimeCleanupRequiredError("容器启动失败且尚未完成清理", remove_unreturned) from BaseExceptionGroup("启动与清理均失败", [original, cleanup])
            if isinstance(original, SandboxOperationOutcomeUnknownError):
                async def verify_unknown() -> None:
                    """原创建请求未收到回执，删除尝试不能证明不会迟到，需运维核实。"""
                    await remove_unreturned()
                    raise RuntimeUnavailableError('容器启动请求结果待核实，清理责任保留')

                raise RuntimeCleanupRequiredError('容器启动请求结果待核实', verify_unknown) from original
            if isinstance(original, SandboxUnavailableError):
                raise RuntimeUnavailableError(f"容器沙盒不可用：{original}") from original
            raise
        tracking = ProcessTrackingExecutor(environment.executor())

        async def close() -> None:
            """先终止任务全部生产进程，仍保留daemon用于冻结后的导出。"""
            try:
                await tracking.close()
            finally:
                await environment.close()

        return SandboxHandle(
            executor=tracking,
            workspace=workspace,
            workspace_inside=config.workdir,
            backend="docker",
            closer=close,
            filesystem=environment.workspace(),
            freezer=close,
        )


__all__ = [
    "DEFAULT_BASE_ENV",
    "KILL_WAIT_SECONDS",
    "DockerSandboxProvider",
    "LocalSandboxProvider",
    "ProcessTrackingExecutor",
    "SandboxHandle",
    "SandboxProvider",
]

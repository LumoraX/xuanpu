"""后端微内核插件宿主（PluginHost）与干净卸载（Clean Unmounting）引擎（ADR 0027）。

- 每个 ``BackendPlugin`` 在 ``mount(ctx)`` 中通过受限 ``PluginContext`` 声明式注册：
  动态 HTTP 路由处理器、管家扩展工具、事件监听器、后台协程任务与清理回调；
- ``unmount(plugin_id)`` 保证：停止接新请求 → 取消并等待插件专属后台任务 →
  注销全部路由/工具/事件监听器 → 执行清理回调，绝不残留僵尸引用；
- ``hot_swap(new_plugin)`` 实现零重启升级，新版本挂载失败自动回滚保留旧版本。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.plugins.collections import PluginCollectionStore
from server.core.plugins.models import (
    BackendPluginManifest,
    PluginHealth,
    PluginRecord,
    PluginState,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from server.core.tenancy import Principal
    from server.platform import Platform

    PluginHttpHandler = Callable[[Principal, str, dict[str, Any]], Awaitable[dict[str, Any]]]
    PluginEventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
    PluginToolHandler = Callable[[Principal, dict[str, Any]], Awaitable[dict[str, Any]]]
    CleanupCallback = Callable[[], Awaitable[None]]
else:
    PluginHttpHandler = Any
    PluginEventHandler = Any
    PluginToolHandler = Any
    CleanupCallback = Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginToolDef:
    """后端插件注册给管家或内部调用的工具定义。"""

    plugin_id: str
    name: str
    description: str
    parameters: dict[str, Any]
    required: tuple[str, ...]
    handler: PluginToolHandler
    parallel: bool = True


class BackendPlugin(Protocol):
    """所有非内核后端模块/扩展的统一插件协议。"""

    manifest: BackendPluginManifest

    async def mount(self, ctx: PluginContext) -> None:
        """向宿主注册路由、工具、事件监听与后台任务。"""
        ...

    async def health(self) -> PluginHealth:
        """执行健康探活，供 L0/L3 监控与回退前预检使用。"""
        ...

    async def unmount(self, *, drain_timeout: float = 15.0) -> None:
        """释放插件内部私有资源（宿主将同步完成路由/事件/任务的自动回收）。"""
        ...


class PluginContext:
    """注入给单个插件的受限能力句柄，同时精确登记其注册的所有资源以便干净卸载。"""

    def __init__(
        self,
        host: PluginHost,
        manifest: BackendPluginManifest,
        collections: PluginCollectionStore,
    ) -> None:
        """绑定宿主引用、插件清单与隔离集合存储。"""
        self._host = host
        self.manifest = manifest
        self.collections = collections
        self._routes: dict[tuple[str, str], PluginHttpHandler] = {}
        self._tools: dict[str, PluginToolDef] = {}
        self._events: dict[str, list[PluginEventHandler]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._cleanups: list[CleanupCallback] = []
        self._accepting = True

    @property
    def platform(self) -> Platform:
        """返回平台微内核引用（仅限 ``in_process_verified`` 插件使用）。"""
        if self.manifest.trust_level != "in_process_verified":
            raise InvalidRequestError("isolated_worker 插件禁止直接访问宿主 Platform 实例")
        return self._host.platform

    def add_route(self, method: str, subpath: str, handler: PluginHttpHandler) -> None:
        """注册一条动态插件 HTTP 路由（挂在 ``/v1/plugins/ext/{plugin_id}/{subpath}`` 下）。"""
        norm_method = method.strip().upper()
        if norm_method not in ("GET", "POST", "PUT", "DELETE"):
            raise InvalidRequestError(f"不支持的插件路由方法：{method!r}")
        norm_path = subpath.strip().strip("/")
        self._routes[(norm_method, norm_path)] = handler

    def add_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: PluginToolHandler,
        *,
        required: tuple[str, ...] = (),
        parallel: bool = True,
    ) -> None:
        """注册一条带插件命名空间前缀的工具（供管家或跨组件调用）。"""
        full_name = name if name.startswith(f"plugin__{self.manifest.plugin_id}__") else (
            f"plugin__{self.manifest.plugin_id}__{name}"
        )
        self._tools[full_name] = PluginToolDef(
            plugin_id=self.manifest.plugin_id,
            name=full_name,
            description=description,
            parameters=parameters,
            required=required,
            handler=handler,
            parallel=parallel,
        )

    def on_event(self, event_name: str, handler: PluginEventHandler) -> None:
        """订阅宿主事件总线上的指定事件。"""
        self._events.setdefault(event_name, []).append(handler)

    def spawn_task(self, coro: Awaitable[Any], *, name: str) -> asyncio.Task[Any]:
        """在插件生命周期作用域内拉起后台协程任务，卸载时自动取消并回收。"""
        task: asyncio.Task[Any] = asyncio.create_task(
            coro,  # type: ignore[arg-type]
            name=f"plugin:{self.manifest.plugin_id}:{name}",
        )
        self._tasks.append(task)
        return task

    def add_cleanup(self, callback: CleanupCallback) -> None:
        """登记额外的异步清理钩子，卸载时按 LIFO 顺序执行。"""
        self._cleanups.append(callback)

    async def _teardown(self, *, drain_timeout: float) -> None:
        """强制收回该插件登记的全部任务、路由、工具、监听与清理钩子。"""
        self._accepting = False
        self._routes.clear()
        self._tools.clear()
        self._events.clear()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(0.5, drain_timeout)):
                    await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for cb in reversed(self._cleanups):
            try:
                await cb()
            except Exception:
                logger.warning("插件 %s 清理回调异常", self.manifest.plugin_id, exc_info=True)
        self._cleanups.clear()


@dataclass
class _MountedEntry:
    """宿主内部维护的单个已登记插件运行槽。"""

    plugin: BackendPlugin
    state: PluginState = "registered"
    ctx: PluginContext | None = None
    mounted_at: datetime | None = None
    last_error: str | None = None


class PluginHost:
    """玄圃微内核后端插件宿主：统一管理插件注册、挂载、热替换、事件分发与干净卸载。"""

    def __init__(
        self, platform: Platform, *, data_dir: Path, now: Callable[[], float] = time.time,
    ) -> None:
        """绑定平台微内核、数据目录与时钟。"""
        self.platform = platform
        self._now = now
        self.collections = PluginCollectionStore(
            data_dir, backend=platform.store.plugin_collection_store, now=now,
        )
        self._entries: dict[str, _MountedEntry] = {}
        self._lock = asyncio.Lock()

    def register(self, plugin: BackendPlugin) -> None:
        """登记一个后端插件（尚未挂载）；重名注册抛出 ConflictError。"""
        pid = plugin.manifest.plugin_id
        if pid in self._entries:
            raise ConflictError(f"插件 {pid!r} 已登记")
        self._entries[pid] = _MountedEntry(plugin=plugin)

    async def mount(self, plugin_id: str) -> PluginRecord:
        """挂载指定已登记插件；失败时自动回滚清理半挂载资源。"""
        async with self._lock:
            entry = self._entries.get(plugin_id)
            if entry is None:
                raise NotFoundError(f"插件不存在：{plugin_id}")
            if entry.state == "mounted":
                return self._record_of(entry)
            ctx = PluginContext(self, entry.plugin.manifest, self.collections)
            try:
                await entry.plugin.mount(ctx)
            except Exception as exc:
                await ctx._teardown(drain_timeout=5.0)
                entry.state = "failed"
                entry.last_error = str(exc)
                logger.exception("挂载后端插件 %s 失败", plugin_id)
                raise
            entry.ctx = ctx
            entry.state = "mounted"
            entry.last_error = None
            entry.mounted_at = datetime.fromtimestamp(self._now(), UTC)
            return self._record_of(entry)

    async def mount_all(self) -> list[PluginRecord]:
        """按登记顺序挂载全部处于 registered 状态的后端插件。"""
        mounted: list[PluginRecord] = []
        for pid in list(self._entries):
            entry = self._entries[pid]
            if entry.state == "registered":
                mounted.append(await self.mount(pid))
        return mounted

    async def unmount(self, plugin_id: str, *, drain_timeout: float = 15.0) -> PluginRecord:
        """干净卸载指定插件：排空任务、注销全部路由/工具/监听器并释放私有资源。"""
        async with self._lock:
            entry = self._entries.get(plugin_id)
            if entry is None:
                raise NotFoundError(f"插件不存在：{plugin_id}")
            if entry.state != "mounted":
                entry.state = "unmounted"
                return self._record_of(entry)
            entry.state = "draining"
            try:
                await entry.plugin.unmount(drain_timeout=drain_timeout)
            except Exception:
                logger.warning("插件 %s unmount 钩子异常，继续强制回收句柄", plugin_id, exc_info=True)
            if entry.ctx is not None:
                await entry.ctx._teardown(drain_timeout=drain_timeout)
                entry.ctx = None
            entry.state = "unmounted"
            return self._record_of(entry)

    async def hot_swap(self, new_plugin: BackendPlugin, *, drain_timeout: float = 15.0) -> PluginRecord:
        """零重启热替换同 ID 插件：新版本挂载失败则自动保留旧版本在线，不中断服务。"""
        pid = new_plugin.manifest.plugin_id
        async with self._lock:
            old_entry = self._entries.get(pid)
            new_ctx = PluginContext(self, new_plugin.manifest, self.collections)
            try:
                await new_plugin.mount(new_ctx)
            except Exception:
                await new_ctx._teardown(drain_timeout=5.0)
                logger.exception("热替换插件 %s@%s 失败，保持旧版本运行", pid, new_plugin.manifest.version)
                raise

            # 新版本已就绪，先原子切换引用，再干净卸载旧版本
            new_entry = _MountedEntry(
                plugin=new_plugin,
                state="mounted",
                ctx=new_ctx,
                mounted_at=datetime.fromtimestamp(self._now(), UTC),
            )
            self._entries[pid] = new_entry
            if old_entry is not None and old_entry.state == "mounted":
                old_entry.state = "draining"
                try:
                    await old_entry.plugin.unmount(drain_timeout=drain_timeout)
                except Exception:
                    logger.warning("热替换卸载旧插件 %s 异常", pid, exc_info=True)
                if old_entry.ctx is not None:
                    await old_entry.ctx._teardown(drain_timeout=drain_timeout)
                    old_entry.ctx = None
            return self._record_of(new_entry)

    async def dispatch_http(
        self, plugin_id: str, method: str, subpath: str, who: Principal, payload: dict[str, Any],
    ) -> dict[str, Any]:
        """按运行时路由表分派插件动态 HTTP 请求；已卸载或未注册路由返回 404。"""
        entry = self._entries.get(plugin_id)
        if entry is None or entry.state != "mounted" or entry.ctx is None or not entry.ctx._accepting:
            raise NotFoundError(f"插件未挂载或不存在：{plugin_id}")
        key = (method.strip().upper(), subpath.strip().strip("/"))
        handler = entry.ctx._routes.get(key)
        if handler is None:
            raise NotFoundError(f"插件 {plugin_id} 未提供路由 {key[0]} /{key[1]}")
        return await handler(who, key[1], payload)

    async def emit_event(self, event_name: str, payload: dict[str, Any]) -> int:
        """向所有已挂载且订阅了 ``event_name`` 的插件广播事件，返回成功触发次数。"""
        invoked = 0
        for entry in list(self._entries.values()):
            if entry.state != "mounted" or entry.ctx is None:
                continue
            for handler in list(entry.ctx._events.get(event_name) or ()):
                try:
                    await handler(event_name, payload)
                    invoked += 1
                except Exception:
                    logger.warning(
                        "插件 %s 处理事件 %s 失败",
                        entry.plugin.manifest.plugin_id, event_name, exc_info=True,
                    )
        return invoked

    def list_tools(self) -> list[PluginToolDef]:
        """收集全部已挂载插件暴露的工具定义。"""
        tools: list[PluginToolDef] = []
        for entry in self._entries.values():
            if entry.state == "mounted" and entry.ctx is not None:
                tools.extend(entry.ctx._tools.values())
        return tools

    async def call_tool(self, tool_name: str, who: Principal, args: dict[str, Any]) -> dict[str, Any]:
        """调用指定插件工具。"""
        for entry in self._entries.values():
            if entry.state == "mounted" and entry.ctx is not None:
                tool = entry.ctx._tools.get(tool_name)
                if tool is not None:
                    return await tool.handler(who, args)
        raise NotFoundError(f"插件工具不存在或已卸载：{tool_name}")

    def list_plugins(self) -> list[PluginRecord]:
        """返回全部已登记后端插件的运行态快照。"""
        return [self._record_of(entry) for entry in self._entries.values()]

    async def health_report(self) -> dict[str, PluginHealth]:
        """对所有已挂载插件执行健康探活。"""
        report: dict[str, PluginHealth] = {}
        now_dt = datetime.fromtimestamp(self._now(), UTC)
        for pid, entry in list(self._entries.items()):
            if entry.state != "mounted":
                report[pid] = PluginHealth(ok=False, detail=f"state={entry.state}", checked_at=now_dt)
                continue
            try:
                report[pid] = await entry.plugin.health()
            except Exception as exc:
                report[pid] = PluginHealth(ok=False, detail=str(exc)[:200], checked_at=now_dt)
        return report

    async def close(self) -> None:
        """平台停机时按逆序干净卸载全部已挂载插件。"""
        for pid in reversed(list(self._entries)):
            if self._entries[pid].state == "mounted":
                await self.unmount(pid, drain_timeout=10.0)

    def _record_of(self, entry: _MountedEntry) -> PluginRecord:
        """构造单个插件的只读快照。"""
        m = entry.plugin.manifest
        ctx = entry.ctx
        routes = tuple(sorted(f"{meth} /{path}" for meth, path in ctx._routes)) if ctx else ()
        tools = tuple(sorted(ctx._tools)) if ctx else ()
        events = tuple(sorted(ctx._events)) if ctx else ()
        return PluginRecord(
            plugin_id=m.plugin_id,
            version=m.version,
            display_name=m.display_name,
            description=m.description,
            trust_level=m.trust_level,
            state=entry.state,
            permissions=m.permissions,
            collections=m.collections,
            routes=routes,
            tools=tools,
            events=events,
            last_error=entry.last_error,
            mounted_at=entry.mounted_at,
        )

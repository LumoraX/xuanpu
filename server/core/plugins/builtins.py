"""平台内置 L2 后端插件与前端内置插件清单（ADR 0027）。

展示「微内核 + 插件」的标准挂载方式：非内核能力（如自定义集合工作区插件、前端富渲染器插件）
均通过统一插件协议登记，支持独立版本演进与干净卸载。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from server.core.errors import InvalidRequestError
from server.core.plugins.models import (
    BackendPluginManifest,
    PluginHealth,
    UiPluginDescriptor,
)

if TYPE_CHECKING:
    from server.core.plugins.host import PluginContext
    from server.core.tenancy import Principal


# L2 内置前端富交互插件清单（由 web/static/builtin-plugins.js 实现并挂载到 4 类标准槽位）
BUILTIN_UI_PLUGINS: tuple[UiPluginDescriptor, ...] = (
    UiPluginDescriptor(
        plugin_id="builtin:mermaid-svg",
        title="Mermaid 离线矢量图渲染",
        slot="message-renderer",
        mode="declarative",
        source_layer="L2_builtin",
        version="1.0.0",
        mime_types=("text/vnd.mermaid",),
    ),
    UiPluginDescriptor(
        plugin_id="builtin:unified-diff",
        title="行级彩色 Diff 审阅器",
        slot="message-renderer",
        mode="declarative",
        source_layer="L2_builtin",
        version="1.0.0",
        mime_types=("text/x-diff",),
    ),
    UiPluginDescriptor(
        plugin_id="builtin:declarative-ui",
        title="声明式业务卡片与指标看板",
        slot="message-renderer",
        mode="declarative",
        source_layer="L2_builtin",
        version="1.0.0",
        mime_types=("application/vnd.xuanpu.ui+json",),
    ),
    UiPluginDescriptor(
        plugin_id="builtin:sandbox-iframe",
        title="沙盒交互式产物与插件视图",
        slot="artifact-viewer",
        mode="sandbox_iframe",
        source_layer="L2_builtin",
        version="1.0.0",
        mime_types=("text/html", "image/svg+xml"),
        bridge_permissions=("artifact.read", "collection.read", "collection.write", "workspace.propose"),
    ),
    UiPluginDescriptor(
        plugin_id="builtin:plugin-manager",
        title="插件中心",
        slot="aux-tab",
        mode="declarative",
        source_layer="L2_builtin",
        version="1.0.0",
    ),
    UiPluginDescriptor(
        plugin_id="builtin:math-formula",
        title="数学公式离线排版",
        slot="message-renderer",
        mode="declarative",
        source_layer="L2_builtin",
        version="1.0.0",
        mime_types=("text/x-latex",),
    ),
    UiPluginDescriptor(
        plugin_id="builtin:code-highlight",
        title="离线代码语法高亮",
        slot="message-renderer",
        mode="declarative",
        source_layer="L2_builtin",
        version="1.0.0",
    ),
)


class CustomRecordsPlugin:
    """内置 L2 示例后端插件：向员工与前端插件开放零 DDL 自定义结构化记录集合（notes/boards）。"""

    manifest = BackendPluginManifest(
        plugin_id="custom-records",
        version="1.0.0",
        display_name="自定义结构化集合",
        description="基于内核零 DDL PluginCollectionStore 的可进化自定义业务记录插件。",
        trust_level="in_process_verified",
        permissions=("collection.read", "collection.write"),
        collections=("items",),
    )

    def __init__(self) -> None:
        """初始化插件私有状态。"""
        self._ctx: PluginContext | None = None

    async def mount(self, ctx: PluginContext) -> None:
        """挂载动态路由与管家工具。"""
        self._ctx = ctx
        ctx.add_route("GET", "items", self._list_items)
        ctx.add_route("POST", "items", self._put_item)
        ctx.add_route("DELETE", "items", self._delete_item)
        ctx.add_tool(
            "list_items",
            "读取本人在 custom-records 插件集合中的自定义结构化记录。",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            self._tool_list,
        )

    async def health(self) -> PluginHealth:
        """确认插件句柄已挂载。"""
        return PluginHealth(
            ok=self._ctx is not None,
            detail="mounted" if self._ctx is not None else "unmounted",
            checked_at=datetime.now(UTC),
        )

    async def unmount(self, *, drain_timeout: float = 15.0) -> None:
        """清空私有句柄，完成干净卸载。"""
        _ = drain_timeout
        self._ctx = None

    async def _list_items(
        self, who: Principal, _subpath: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        assert self._ctx is not None
        limit = int(payload.get("limit") or 50)
        rows = await self._ctx.collections.list(who, self.manifest.plugin_id, "items", limit=limit)
        return {"items": [r.model_dump(mode="json") for r in rows]}

    async def _put_item(
        self, who: Principal, _subpath: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        assert self._ctx is not None
        data = payload.get("data")
        if not isinstance(data, dict):
            raise InvalidRequestError("缺少合法的 data 对象")
        record_id = payload.get("record_id")
        expected_version = payload.get("expected_version")
        rec = await self._ctx.collections.put(
            who,
            self.manifest.plugin_id,
            "items",
            data,
            record_id=str(record_id) if isinstance(record_id, str) and record_id.strip() else None,
            expected_version=int(expected_version) if isinstance(expected_version, int) else None,
        )
        return rec.model_dump(mode="json")

    async def _delete_item(
        self, who: Principal, _subpath: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        assert self._ctx is not None
        record_id = payload.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise InvalidRequestError("缺少 record_id")
        await self._ctx.collections.delete(who, self.manifest.plugin_id, "items", record_id.strip())
        return {"deleted": True, "record_id": record_id.strip()}

    async def _tool_list(self, who: Principal, args: dict[str, Any]) -> dict[str, Any]:
        return await self._list_items(who, "items", args)

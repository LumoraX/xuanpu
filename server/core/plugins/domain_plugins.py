"""将核心非内核业务域按统一 ``BackendPlugin`` 契约挂载为可演进 L2 后端插件（ADR 0027）。

首批迁入微内核插件总线的核心业务域：
- ``topic-watch``：长期静默追踪（底层共享 ``PluginCollectionStore``）；
- ``proactive-briefing``：每日晨报、会前速览与持久化习惯校准档案；
- ``people-graph``：人脉与项目图谱及语义+图谱+时效混合召回。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from server.core.errors import InvalidRequestError
from server.core.plugins.models import BackendPluginManifest, PluginHealth

if TYPE_CHECKING:
    from server.core.plugins.host import PluginContext
    from server.core.tenancy import Principal


class TopicWatchBackendPlugin:
    """L2 业务域插件：长期主题静默追踪。"""

    manifest = BackendPluginManifest(
        plugin_id="topic-watch",
        version="1.0.0",
        display_name="长期主题静默追踪",
        description="基于共享 PluginCollectionStore 的低频静默追踪与基线比对插件。",
        trust_level="in_process_verified",
        permissions=("collection.read", "collection.write"),
        collections=("watches",),
    )

    def __init__(self) -> None:
        """初始化私有上下文引用。"""
        self._ctx: PluginContext | None = None

    async def mount(self, ctx: PluginContext) -> None:
        """挂载动态路由与工具。"""
        self._ctx = ctx
        ctx.add_route("GET", "list", self._list)
        ctx.add_route("POST", "create", self._create)
        ctx.add_route("DELETE", "delete", self._delete)
        ctx.add_tool("list", "列出本人登记的全部长期静默追踪主题。", {}, self._tool_list)

    async def health(self) -> PluginHealth:
        """探活检查。"""
        ok = self._ctx is not None
        return PluginHealth(ok=ok, detail="mounted" if ok else "unmounted", checked_at=datetime.now(UTC))

    async def unmount(self, *, drain_timeout: float = 15.0) -> None:
        """干净卸载。"""
        _ = drain_timeout
        self._ctx = None

    async def _list(self, who: Principal, _sub: str, _payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        items = await self._ctx.platform.butler.topic_watches.list(who)
        return {"items": [i.model_dump(mode="json") for i in items]}

    async def _create(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        topic = str(payload.get("topic") or "").strip()
        if not topic:
            raise InvalidRequestError("缺少 topic")
        created = await self._ctx.platform.butler.topic_watches.create(who, topic)
        return created.model_dump(mode="json")

    async def _delete(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        watch_id = str(payload.get("watch_id") or "").strip()
        if not watch_id:
            raise InvalidRequestError("缺少 watch_id")
        await self._ctx.platform.butler.topic_watches.delete(who, watch_id)
        return {"deleted": True, "watch_id": watch_id}

    async def _tool_list(self, who: Principal, args: dict[str, Any]) -> dict[str, Any]:
        return await self._list(who, "list", args)


class ProactiveBriefingBackendPlugin:
    """L2 业务域插件：主动晨报、会前速览与持久化隐式习惯校准。"""

    manifest = BackendPluginManifest(
        plugin_id="proactive-briefing",
        version="1.0.0",
        display_name="主动简报与注意力门控",
        description="提供每日要点晨报、会前 30 分钟速览与持久化注意力/习惯校准状态。",
        trust_level="in_process_verified",
        permissions=("collection.read", "collection.write"),
        collections=("budget", "habit"),
    )

    def __init__(self) -> None:
        """初始化私有上下文引用。"""
        self._ctx: PluginContext | None = None

    async def mount(self, ctx: PluginContext) -> None:
        """挂载动态简报与习惯校准路由。"""
        self._ctx = ctx
        ctx.add_route("GET", "daily", self._daily)
        ctx.add_route("GET", "pre-meeting", self._pre_meeting)
        ctx.add_route("GET", "habit", self._habit)
        ctx.add_route("POST", "habit/ignore", self._record_ignore)

    async def health(self) -> PluginHealth:
        """探活检查。"""
        ok = self._ctx is not None
        return PluginHealth(ok=ok, detail="mounted" if ok else "unmounted", checked_at=datetime.now(UTC))

    async def unmount(self, *, drain_timeout: float = 15.0) -> None:
        """干净卸载。"""
        _ = drain_timeout
        self._ctx = None

    async def _daily(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        tz = str(payload.get("timezone") or "Asia/Shanghai")
        deliver = str(payload.get("deliver") or "").lower() in ("true", "1", "yes")
        return await self._ctx.platform.butler.briefings.build_daily_brief(
            who, timezone=tz, deliver=deliver,
        )

    async def _pre_meeting(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        tz = str(payload.get("timezone") or "Asia/Shanghai")
        window = int(payload.get("window_minutes") or 30)
        deliver = str(payload.get("deliver") or "").lower() in ("true", "1", "yes")
        items = await self._ctx.platform.butler.briefings.build_pre_meeting_briefs(
            who, timezone=tz, window_minutes=window, deliver=deliver,
        )
        return {"items": items}

    async def _habit(self, who: Principal, _sub: str, _payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        habit = await self._ctx.platform.butler.briefings.get_habit(who)
        return habit.model_dump(mode="json")

    async def _record_ignore(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        topic_kind = str(payload.get("topic_kind") or "").strip()
        if not topic_kind:
            raise InvalidRequestError("缺少 topic_kind")
        reset = bool(payload.get("reset", False))
        habit = await self._ctx.platform.butler.briefings.record_habit_ignored(
            who, topic_kind, reset=reset,
        )
        return habit.model_dump(mode="json")


class PeopleGraphBackendPlugin:
    """L2 业务域插件：人脉与项目知识图谱及三路混合召回。"""

    manifest = BackendPluginManifest(
        plugin_id="people-graph",
        version="1.0.0",
        display_name="人脉与项目图谱",
        description="封装本人人脉图谱查询与语义+图谱+时效混合记忆召回能力。",
        trust_level="in_process_verified",
        permissions=("people.read",),
    )

    def __init__(self) -> None:
        """初始化私有上下文引用。"""
        self._ctx: PluginContext | None = None

    async def mount(self, ctx: PluginContext) -> None:
        """挂载图谱混合召回路由与工具。"""
        self._ctx = ctx
        ctx.add_route("GET", "recall", self._recall)
        ctx.add_tool(
            "hybrid_recall",
            "跨人物、公司、记录、事项、待办与文件的语义+图谱+时效混合召回。",
            {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            self._tool_recall,
            required=("query",),
        )

    async def health(self) -> PluginHealth:
        """探活检查。"""
        ok = self._ctx is not None
        return PluginHealth(ok=ok, detail="mounted" if ok else "unmounted", checked_at=datetime.now(UTC))

    async def unmount(self, *, drain_timeout: float = 15.0) -> None:
        """干净卸载。"""
        _ = drain_timeout
        self._ctx = None

    async def _recall(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        from server.domains.people.hybrid_recall import hybrid_recall_personal

        query = str(payload.get("query") or "").strip()
        if not query:
            raise InvalidRequestError("缺少 query 关键词")
        limit = max(1, min(int(payload.get("limit") or 10), 20))
        hits = await hybrid_recall_personal(
            self._ctx.platform, who, query, now=self._ctx.platform._now(), limit=limit,
        )
        return {"query": query, "items": hits}

    async def _tool_recall(self, who: Principal, args: dict[str, Any]) -> dict[str, Any]:
        return await self._recall(who, "recall", args)

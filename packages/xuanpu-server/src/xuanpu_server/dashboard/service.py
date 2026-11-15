"""员工概览：四块各自取数，数量在授权过滤后计算；单块失败只影响该块；不调模型。

- 今天：本人当天到期的活动提醒、与当天有交集的日程、当天及之前到期的未完成待办。
- 等我处理：未处理的助手草稿、管家待本人处理项（管家开启时）、等本人的交办工作、
  轮到本人决定的组织审批（审批开启时）。
- 进行中：本人活动的执行 trace（执行追踪开启时；关闭时这一块不出现）。
- 未读通知：通知中心里本人未读的站内通知。

身份不合法（机器身份、停用员工、暂停租户）整个请求被拒，不返回任何块。
响应另带确定性提示 ``tips``（最多 3 条，见 ``dashboard/tips.py``）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xuanpu_server.dashboard.models import (
    BLOCK_ORDER,
    SUMMARY_MAX,
    Block,
    BlockKey,
    Summary,
    failed_view,
    short,
)
from xuanpu_server.dashboard.tips import build_tips
from xuanpu_server.errors import InvalidRequestError, PlatformError
from xuanpu_server.tracing.models import TraceOwner

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal

logger = logging.getLogger(__name__)
_PAGE = 100
_MAX_PAGES = 20
_WAITING_PENDING = frozenset({"open", "forwarding"})


def day_bounds(now: float, timezone: str) -> tuple[float, float]:
    """本人时区里「今天」的起止（UTC 秒）。时区名无效是请求错误。"""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidRequestError("时区无效") from exc
    local = datetime.fromtimestamp(now, zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _top(items: list[Summary]) -> tuple[Summary, ...]:
    """按时间（无时间的排最后）取前 5 条。"""
    ordered = sorted(items, key=lambda s: (s.at is None, s.at or 0.0, s.ref))
    return tuple(ordered[:SUMMARY_MAX])


class DashboardService:
    """组装概览；只读，各块互不依赖。"""

    def __init__(self, platform: Platform, *, now: Callable[[], float]) -> None:
        """依赖平台已有服务。"""
        self._p = platform
        self._now = now

    async def overview(self, who: Principal, *, timezone: str | None = None) -> dict[str, Any]:
        """返回启用的块；单块失败标记为 error，其余照常。"""
        tenant, _ = await self._p.workspace.require_employee(who)
        zone = timezone or str((await self._p.workspace.settings(who)).get("timezone"))
        bounds = day_bounds(self._now(), zone)
        readers: dict[BlockKey, Callable[[], Awaitable[Block]]] = {
            "today": lambda: self._today(who, bounds),
            "waiting": lambda: self._waiting(who, tenant),
            "unread": lambda: self._unread(who),
        }
        if self._p.runtimes.defaults.execution_trace.allows(tenant):
            readers["in_progress"] = lambda: self._in_progress(who)
        keys = [key for key in BLOCK_ORDER if key in readers]
        results = await asyncio.gather(*(readers[key]() for key in keys), return_exceptions=True)
        blocks = [self._view(key, result) for key, result in zip(keys, results, strict=True)]
        tips = build_tips(blocks, artisans=await self._artisan_names(who, tenant),
                          first_use=not await self._p.workspace.messages(who, 0, 1, latest=True))
        return {"timezone": zone, "day_start": bounds[0], "day_end": bounds[1], "blocks": blocks,
                "tips": [tip.view() for tip in tips]}

    async def _artisan_names(self, who: Principal, tenant: str) -> list[str]:
        """管家开启时本人可用的匠人名（去重、按出现顺序）；读不出只记日志，提示照常给通用项。"""
        if not self._p.butler_config.allows(tenant):
            return []
        try:
            found = await self._p.artisans.available(who)
        except PlatformError as exc:
            logger.warning("概览提示读取可用匠人失败：%s", exc.code)
            return []
        return list(dict.fromkeys(artisan.ref.package for artisan in found))

    @staticmethod
    def _view(key: BlockKey, result: Block | BaseException) -> dict[str, Any]:
        """成功给数据；平台错误给错误码；意外错误记日志后给 internal_error。"""
        if isinstance(result, Block):
            return result.view()
        if isinstance(result, PlatformError):
            return failed_view(key, result.code)
        if not isinstance(result, Exception):
            raise result
        logger.error("概览块 %s 读取失败", key, exc_info=result)
        return failed_view(key, "internal_error")

    async def _today(self, who: Principal, bounds: tuple[float, float]) -> Block:
        """当天的提醒、日程与到期待办。"""
        start, end = bounds
        found = [Summary("reminder", r.reminder_id, short(r.title), r.due_at)
                 for r in await self._p.reminders.list(who, status="active")
                 if start <= r.due_at < end]
        for item in await self._p.workspace.list(who, kind="event"):
            data = item["data"]
            if data["start_at"] < end and data["end_at"] > start:
                found.append(Summary("event", item["id"], short(data["title"]), data["start_at"]))
        for item in await self._p.workspace.list(who, kind="todo"):
            data = item["data"]
            due = data.get("due_at")
            if data.get("status") == "open" and due is not None and due < end:
                found.append(Summary("todo", item["id"], short(data["title"]), due,
                                     {"overdue": due < start}))
        return Block("today", len(found), _top(found))

    async def _waiting(self, who: Principal, tenant: str) -> Block:
        """等本人处理的四类来源；关闭的来源不计入。"""
        found = [Summary("proposal", str(row["request_id"]).removesuffix(":assistant"),
                         short(str(row.get("text") or "待确认的草稿")),
                         _float(row.get("created_at")))
                 for row in await self._p.workspace.pending_proposals(who)]
        if self._p.butler_config.allows(tenant):
            for pending in await self._p.butler.pending.list(who):
                if pending.state in _WAITING_PENDING:
                    found.append(Summary(pending.kind, pending.pending_id, short(pending.question),
                                         pending.created_at.timestamp()))
        found.extend(await self._waiting_tasks(who))
        if self._p.approvals.config.allows(tenant):
            found.extend(await self._waiting_approvals(who))
        return Block("waiting", len(found), _top(found))

    async def _waiting_tasks(self, who: Principal) -> list[Summary]:
        """等本人确认的交办工作。"""
        found: list[Summary] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            page = await self._p.work_tasks.store.list(who, limit=_PAGE, after_id=after)
            found.extend(Summary("work_task", t.task_id, short(t.text or "交办工作"), t.updated_at)
                         for t in page if t.status == "waiting_user")
            if len(page) < _PAGE:
                return found
            after = page[-1].task_id
        raise PlatformError("交办工作过多，概览无法完整统计")

    async def _waiting_approvals(self, who: Principal) -> list[Summary]:
        """轮到本人审批的请求（审批列表本身已按本人可见性过滤）。"""
        found: list[Summary] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page = await self._p.approvals.listing(who, limit=_PAGE, cursor=cursor)
            items = page.get("items")
            for record in items if isinstance(items, list) else []:
                actions = record.get("allowed_actions") if isinstance(record, dict) else None
                if isinstance(record, dict) and isinstance(actions, list) and "approve" in actions:
                    found.append(Summary("approval_request", str(record["id"]), "审批请求",
                                         _float(record.get("created_at"))))
            next_cursor = page.get("next_cursor")
            if not isinstance(next_cursor, str):
                return found
            cursor = next_cursor
        raise PlatformError("审批请求过多，概览无法完整统计")

    async def _in_progress(self, who: Principal) -> Block:
        """本人活动的 trace：总数来自存储，摘要带是否在等本人、记录是否完整。"""
        tenant, user = await self._p.workspace.require_employee(who)
        owner = TraceOwner(tenant_id=tenant, user_id=user)
        store = self._p.store.trace_store
        page = await store.list(owner, active=True, limit=SUMMARY_MAX)
        items = []
        for trace in page.items:
            view = await store.get(owner, trace.trace_id)
            waiting = any(span.state == "waiting_input" for span in view.spans)
            items.append(Summary("trace", trace.trace_id, short(trace.title),
                                 trace.created_at.timestamp(),
                                 {"waiting_me": waiting, "incomplete": trace.incomplete}))
        return Block("in_progress", page.total, tuple(items))

    async def _unread(self, who: Principal) -> Block:
        """通知中心里本人未读的站内通知。"""
        tenant, user = await self._p.reminders.require_employee(who)
        total, latest = await self._p.reminders.store.unread_notifications(
            tenant, user, SUMMARY_MAX)
        items = tuple(Summary("notification", str(n.notification_id), short(n.title), n.created_at)
                      for n in latest)
        return Block("unread", total, items)


def _float(value: object) -> float | None:
    """时间字段可能缺失；只接受数值。"""
    return float(value) if isinstance(value, int | float) else None

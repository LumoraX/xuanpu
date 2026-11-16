"""待本人处理项与把答复转给匠人（R5-C2）。

- 员工答复问题：``POST /v1/assistant/pending/{id}/answer``（或下一句话被管家认出是答复）。
  答复原样转给匠人会话，问题项标为已答复，派工回到运行中。
- 审批类待处理项不经这里答复：员工在既有的待确认入口处理，对账器看到宿主继续后自动收尾。
- 转给匠人可能在发出后、记账前被中断：先把项标为 ``forwarding``，再发；重试时按消息里的
  编号查会话审计，已发过就只补记账，不重发。审计在发出后才写，两者之间被终止时仍可能
  重发一次（已知窗口，见架构文档）。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from xuanpu_server.butler.wake_models import AnswerSource
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.errors import ConflictError, InvalidRequestError
from xuanpu_server.tracing.models import TraceOwner

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from xuanpu_server.butler.wake_models import PendingItem
    from xuanpu_server.platform import Platform
    from xuanpu_server.runtime.sessions import Turn
    from xuanpu_server.tenancy import Principal

    Ruler = Callable[[TraceOwner, PendingItem, str, str], Awaitable[None]]

ANSWER_MAX = 2000


def owner_of(who: Principal) -> TraceOwner:
    """员工身份 → 归属。"""
    if who.tenant_id is None or who.user_id is None:
        raise InvalidRequestError("需要员工身份")
    return TraceOwner(tenant_id=who.tenant_id, user_id=who.user_id)


def forward_text(item: PendingItem, answer: str, source: AnswerSource) -> str:
    """转给匠人的消息：带编号（用于查重）与出处。"""
    origin = {"employee": "员工本人答复", "setting": "管家据员工本人设置代答",
              "conversation": "管家据员工本次原话代答"}[source.kind]
    return f"〔{item.pending_id}〕{origin}：{answer}"


class PendingService:
    """待本人处理项的读取与答复。"""

    def __init__(self, platform: Platform, *, now: Callable[[], float]) -> None:
        """依赖由平台注入。"""
        self._platform = platform
        self._now = now
        self._drains: set[asyncio.Task[None]] = set()
        self._ruler: Ruler | None = None

    def _at(self) -> datetime:
        """当前时间（UTC）。"""
        return datetime.fromtimestamp(self._now(), UTC)

    async def list(self, who: Principal) -> tuple[PendingItem, ...]:
        """本人仍待处理的项。"""
        await self._platform.workspace.require_employee(who)
        items = await self._platform.store.wake_store.list_pending(owner_of(who))
        return tuple(i for i in items if i.kind != "proxied")

    async def answer(self, who: Principal, pending_id: str, text: str, request_id: str,
                     ) -> PendingItem:
        """员工答复一个问题项；同一请求标识重试回原结果，不同内容冲突。"""
        await self._platform.workspace.require_employee(who)
        answer = text.strip()
        if not answer or len(answer) > ANSWER_MAX:
            raise InvalidRequestError(f"答复须为 1–{ANSWER_MAX} 个字符")
        store = self._platform.store.wake_store
        item = await store.get_pending(owner_of(who), pending_id)
        if item.kind == "ruling":
            return await self._rule(who, item, answer, request_id)
        if item.answer_request_id is not None:
            if item.answer_request_id != request_id or item.answer != answer:
                raise ConflictError("这个问题已经答复过")
            if item.state == "answered":
                return item
        elif item.kind != "question":
            raise ConflictError("需要你审批的操作请在待确认里处理")
        elif item.state != "open":
            raise ConflictError("这个问题已不再等待答复")
        source = AnswerSource(kind="employee", quote=answer)
        return await self.forward(who, item, answer, source, request_id=request_id)

    def bind_ruling(self, ruler: Ruler) -> None:
        """装配时接上编组裁决（编组执行器晚于本对象构造）。"""
        self._ruler = ruler

    async def _rule(self, who: Principal, item: PendingItem, answer: str, request_id: str,
                    ) -> PendingItem:
        """会诊裁决：先记进编组（幂等），再把裁决项标为已答复；不转给任何匠人。"""
        if self._ruler is None:
            raise RuntimeError("会诊裁决需要编组执行器")
        if item.state not in ("open", "answered"):
            raise ConflictError("这项裁决已不再需要")
        owner = owner_of(who)
        await self._ruler(owner, item, answer, request_id)
        if item.state == "answered":
            return item
        return await self._platform.store.wake_store.update_pending(
            owner, item.pending_id, expected_version=item.version, at=self._at(),
            state="answered", answer=answer, answer_source=AnswerSource(kind="employee", quote=answer),
            answer_request_id=request_id)

    async def forward(self, who: Principal, item: PendingItem, answer: str, source: AnswerSource,
                      *, request_id: str) -> PendingItem:
        """把答复转给匠人会话，派工回到运行中；可安全重试。"""
        store = self._platform.store.wake_store
        owner = owner_of(who)
        if item.state != "forwarding":
            item = await store.update_pending(
                owner, item.pending_id, expected_version=item.version, at=self._at(),
                state="forwarding", answer=answer, answer_source=source,
                answer_request_id=request_id)
        message = forward_text(item, answer, source)
        if not await self._already_sent(who.require_tenant(), item):
            started = await self._platform.sessions.send_message(who, item.host_ref, message)
            self._drain(started)
        await self._resume_dispatch(owner, item.dispatch_id)
        return await store.update_pending(owner, item.pending_id, expected_version=item.version,
                                          at=self._at(), state="answered")

    async def _already_sent(self, tenant: str, item: PendingItem) -> bool:
        """会话审计里是否已有带这个编号的消息。"""
        records = await self._platform.store.query_audit(
            tenant, AuditQuery(session_id=item.host_ref, kind="message_sent", limit=1000))
        marker = f"〔{item.pending_id}〕"
        return any(str(r.data.get("text", "")).startswith(marker) for r in records)

    async def _resume_dispatch(self, owner: TraceOwner, dispatch_id: str) -> None:
        """派工从 asking 回到 running；已在运行中不动。"""
        dispatches = self._platform.dispatches
        record = await dispatches.get(owner, dispatch_id)
        if record.state == "asking":
            await dispatches.update(owner, dispatch_id, expected_version=record.version,
                                    to="running", at=self._at())

    async def resolve_for_dispatch(self, owner: TraceOwner, dispatch_id: str, *,
                                   kinds: tuple[str, ...]) -> None:
        """宿主已继续或已结束：把这次派工仍开着的指定类别项标为已解决。"""
        store = self._platform.store.wake_store
        for item in await store.list_pending(owner, dispatch_id=dispatch_id):
            if item.kind in kinds and item.state == "open":
                await store.update_pending(owner, item.pending_id, expected_version=item.version,
                                           at=self._at(), state="resolved")

    def _drain(self, started: Turn) -> None:
        """读完匠人这一轮的事件流；内容已由事件枢纽进审计与追踪。"""
        async def consume() -> None:
            """逐条丢弃。"""
            async for _ in started.stream.events():
                pass

        task = asyncio.ensure_future(consume())
        self._drains.add(task)
        task.add_done_callback(self._drains.discard)

    async def close(self) -> None:
        """取消仍在读的后台轮次。"""
        tasks = tuple(self._drains)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def view(item: PendingItem) -> dict[str, Any]:
    """接口视图：不回显租户与员工标识。"""
    return {"pending_id": item.pending_id, "dispatch_id": item.dispatch_id, "kind": item.kind,
            "question": item.question, "host": item.host, "host_ref": item.host_ref,
            "state": item.state, "answer": item.answer, "version": item.version,
            "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat()}

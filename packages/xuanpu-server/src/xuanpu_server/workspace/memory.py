"""用于本机的个人工作台内存事务后端。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from xuanpu_server.workspace.models import WorkspaceState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from xuanpu_server.controlplane.models import AuditRecord


class InMemoryWorkspaceStore:
    """按员工加锁，私有副本验证后一次提交。"""

    def __init__(self, audit: Callable[[list[AuditRecord]], None]) -> None:
        """注入同步批量审计，在提交前记录。"""
        self._audit = audit
        self._states: dict[tuple[str, str], WorkspaceState] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @asynccontextmanager
    async def transaction(
        self,
        tenant: str,
        user: str,
        guard: Callable[[], Awaitable[None]],
        request_id: str | None = None,
    ) -> AsyncIterator[WorkspaceState]:
        """复查身份且失败不替换原状态。"""
        from xuanpu_server.controlplane.models import AuditRecord

        owner = (tenant, user)
        async with self._locks.setdefault(owner, asyncio.Lock()):
            await guard()
            original = self._states.get(owner, WorkspaceState())
            state = deepcopy(original)
            state.audits = []
            yield state
            await guard()
            audits = [
                AuditRecord(tenant_id=tenant, user_id=user, source="control", **a)
                for a in state.audits
            ]
            self._audit(audits)
            state.audits = []
            self._states[owner] = deepcopy(state)

    async def messages(
        self,
        tenant: str,
        user: str,
        after_id: int,
        limit: int,
        *,
        matter_id: str | None = None,
        before_id: int | None = None,
        latest: bool = False,
    ) -> list[dict[str, Any]]:
        """返回独立副本，调用者无法修改已保存消息。"""
        state = self._states.get((tenant, user), WorkspaceState())
        found = [
            m
            for m in state.messages
            if m["id"] > after_id
            and (before_id is None or m["id"] < before_id)
            and (matter_id is None or m["matter_id"] == matter_id)
        ]
        return deepcopy(found[-limit:] if latest else found[:limit])

    async def find_message(self, tenant: str, user: str, request_id: str) -> dict[str, Any] | None:
        """利用幂等字典按请求查已保存消息。"""
        state = self._states.get((tenant, user), WorkspaceState())
        request = state.requests.get(request_id)
        if request is None or "role" not in request["result"]:
            return None
        return dict(deepcopy(request["result"]))

    async def pending_proposals(self, tenant: str, user: str) -> list[dict[str, Any]]:
        """用请求索引排除已确认或取消，聊天不会占分页额度。"""
        state = self._states.get((tenant, user), WorkspaceState())
        found: list[dict[str, Any]] = []
        message_requests = {message["request_id"] for message in state.messages}
        for message in reversed(state.messages):
            request_id = message["request_id"]
            if message["role"] != "assistant" or not request_id.endswith(":assistant"):
                continue
            route = message.get("metadata", {}).get("route", {})
            if not isinstance(route, dict) or not any(
                isinstance(route.get(k), dict) for k in ("workspace_draft", "reminder_draft")
            ):
                continue
            base = request_id[:-10]
            decision = state.requests.get("proposal:" + base)
            if decision is not None and decision["result"].get("state") in (
                "confirmed",
                "cancelled",
            ):
                continue
            if base + ":confirmed" in message_requests or base + ":cancelled" in message_requests:
                continue
            found.append(message)
            if len(found) == 100:
                break
        return deepcopy(found)

    async def proposal_result(
        self, tenant: str, user: str, request_id: str
    ) -> dict[str, Any] | None:
        """没有终态聊天也可通过请求索引恢复已提交回执。"""
        state = self._states.get((tenant, user), WorkspaceState())
        decision = state.requests.get("proposal:" + request_id)
        if decision is None or decision["result"].get("state") not in ("confirmed", "cancelled"):
            return None
        return dict(deepcopy(decision["result"]))

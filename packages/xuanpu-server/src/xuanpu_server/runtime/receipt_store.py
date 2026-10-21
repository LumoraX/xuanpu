"""审批回执协议及单进程试用实现。"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol

from xuanpu_server.errors import NotFoundError
from xuanpu_server.runtime.operation_receipts import (
    OperationReceipt,
    decision_fingerprint,
    replay_receipt,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.controlplane.models import AuditRecord, SessionRecord


class ReceiptStore(Protocol):
    """认领、绑定提交和结算均按租户隔离。"""

    async def claim(self, tenant: str, session: str, decisions: dict[str, Any], *, at: float) -> OperationReceipt:
        """原子认领整个决定集合或返回已知提交回执。"""
        ...

    async def submitted(self, tenant: str, operation: str, submission: str, *, at: float) -> None:
        """绑定已提交标识，不覆盖已到达的核销状态。"""
        ...

    async def settle(self, tenant: str, operation: str, state: str, audit: AuditRecord | None, *, at: float) -> None:
        """在同一事务内追加审计并结算决定。"""
        ...

    async def release(self, tenant: str, operation: str) -> None:
        """仅释放可证明从未提交的认领。"""
        ...


class MemoryReceiptStore:
    """无让出点地提交回执和控制面审计，进程退出不保留。"""

    def __init__(self, session: Callable[[str, str], SessionRecord | None], audit: Callable[[AuditRecord], None]) -> None:
        """注入同一个控制面的会话查询与同步审计。"""
        self._session = session
        self._audit = audit
        self._lock = asyncio.Lock()
        self._rows: dict[tuple[str, str, str], OperationReceipt] = {}

    async def claim(self, tenant: str, session: str, decisions: dict[str, Any], *, at: float) -> OperationReceipt:
        """相同请求标识永不因时间经过而自动重新执行。"""
        fingerprint = decision_fingerprint(decisions)
        async with self._lock:
            if self._session(tenant, session) is None:
                raise NotFoundError("会话不存在")
            keys = [(tenant, session, key) for key in sorted(decisions)]
            # rejected 是内核证明未执行的记录，可与本次认领一起覆盖。
            prior = [self._rows[key] for key in keys
                     if key in self._rows and self._rows[key].state != "rejected"]
            if prior:
                return replay_receipt(prior, fingerprint, len(keys))
            receipt = OperationReceipt("op_" + secrets.token_hex(16), fingerprint)
            self._rows.update(dict.fromkeys(keys, receipt))
            return receipt

    async def submitted(self, tenant: str, operation: str, submission: str, *, at: float) -> None:
        """结论可能先于数据库绑定到达，不能把 applied 降回 submitted。"""
        async with self._lock:
            for key, row in list(self._rows.items()):
                if key[0] == tenant and row.operation_id == operation:
                    self._rows[key] = replace(row, submission_id=submission,
                        state="submitted" if row.state == "claimed" else row.state)

    async def settle(self, tenant: str, operation: str, state: str, audit: AuditRecord | None, *, at: float) -> None:
        """先写审计再提交状态；审计失败不留下半次结算。"""
        async with self._lock:
            selected = [(key, row) for key, row in self._rows.items()
                        if key[0] == tenant and row.operation_id == operation]
            changed = [(key, row) for key, row in selected
                       if row.state != state and not (row.state == "applied" and state != "applied")]
            if not changed:
                return
            if audit is not None:
                if audit.tenant_id != tenant:
                    raise ValueError("回执审计租户不匹配")
                self._audit(audit)
            for key, row in changed:
                self._rows[key] = replace(row, state=state)

    async def release(self, tenant: str, operation: str) -> None:
        """提交过或已核销的决定不能通过清理重新执行。"""
        async with self._lock:
            for key, row in list(self._rows.items()):
                if key[0] == tenant and row.operation_id == operation and (row.state == "rejected" or (row.state == "claimed" and row.submission_id is None)):
                    del self._rows[key]

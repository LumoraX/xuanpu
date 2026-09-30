"""会话管理：建会话、发消息、取事件流、送回审批结果、关闭。

一个会话对应内核里的一个线程，由某一个平台实例持有（单写者）。持有权是带期限的租约：
持有者定期续期；持有者崩溃后租约到期，别的实例才能接手，并从会话存储里续接。
第一版会话存储是节点本地文件，跨节点接手要求各节点共用这份存储（共享卷）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import taifeng

from xuanpu_server.controlplane.models import AuditQuery, AuditRecord, SessionRecord
from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    QuotaExceededError,
    RuntimeUnavailableError,
)
from xuanpu_server.ids import new_id
from xuanpu_server.runtime.events import EngineEventHub, SubmissionIndex, TurnStream

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.packages import PackageRegistry
    from xuanpu_server.quota import QuotaGuard
    from xuanpu_server.runtime.assembler import RuntimeAssembler, TenantRuntime
    from xuanpu_server.tenancy import Principal

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 200_000
MAX_METADATA_ITEMS = 16
MAX_METADATA_CHARS = 256


def validate_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    """业务方附加的关联标识：条数与长度有限，键值都是字符串。"""
    if not metadata:
        return {}
    if len(metadata) > MAX_METADATA_ITEMS:
        raise InvalidRequestError(f"metadata 最多 {MAX_METADATA_ITEMS} 项")
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise InvalidRequestError("metadata 的键和值必须都是字符串")
        if not key or len(key) > MAX_METADATA_CHARS or len(value) > MAX_METADATA_CHARS:
            raise InvalidRequestError(f"metadata 的键值长度须在 1 到 {MAX_METADATA_CHARS} 之间")
    return dict(metadata)


@dataclass
class _LiveSession:
    """本实例正持有的一个会话。"""

    record: SessionRecord
    runtime: TenantRuntime
    engine: taifeng.AgentEngine
    hub: EngineEventHub
    last_used: float = 0.0
    in_flight: set[str] = field(default_factory=set)

    @property
    def idle(self) -> bool:
        """是否没有进行中的对话。"""
        return not self.in_flight


@dataclass(frozen=True)
class Turn:
    """已提交的一轮对话。"""

    session_id: str
    submission_id: str
    stream: TurnStream


class SessionManager:
    """会话管理。"""

    def __init__(
        self,
        *,
        store: ControlPlaneStore,
        packages: PackageRegistry,
        runtimes: RuntimeAssembler,
        quota: QuotaGuard,
        audit: AuditLog,
        submissions: SubmissionIndex,
        instance_id: str,
        lease_seconds: float = 60.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            store: 控制面存储。
            packages: 业务包登记处。
            runtimes: 运行时组装器。
            quota: 配额检查。
            audit: 审计写入口。
            submissions: submission → 会话的对照表。
            instance_id: 本实例的标识，写进租约。
            lease_seconds: 租约时长。续期间隔取它的三分之一。
            now: 取当前时间的函数，测试时可替换。
        """
        self._store = store
        self._packages = packages
        self._runtimes = runtimes
        self._quota = quota
        self._audit = audit
        self._submissions = submissions
        self._instance = instance_id
        self._lease_seconds = lease_seconds
        self._now = now
        self._live: dict[tuple[str, str], _LiveSession] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._heartbeat: asyncio.Task[None] | None = None
        self._draining = False

    async def start(self) -> None:
        """启动租约续期。"""
        if self._heartbeat is None:
            self._heartbeat = asyncio.ensure_future(self._renew_leases())

    async def create(
        self,
        principal: Principal,
        *,
        package: str,
        version: str | None = None,
        entry: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> SessionRecord:
        """新建会话。版本在此钉死：之后业务包切换生效版本不影响这个会话。"""
        tenant_id = principal.require_tenant()
        labels = validate_metadata(metadata)
        await self._quota.check_new_session(tenant_id)
        resolved = await self._packages.resolve(tenant_id, package, version)
        entries = resolved.manifest.spec.skills.entries
        chosen = entry or entries[0]
        if chosen not in entries:
            raise InvalidRequestError(
                f"{chosen!r} 不是业务包 {package!r} 的入口；可选：{', '.join(entries)}"
            )
        now = self._now()
        record = SessionRecord(
            session_id=new_id("ses"),
            tenant_id=tenant_id,
            package=resolved.record.name,
            version=resolved.record.version,
            entry=chosen,
            created_at=now,
            updated_at=now,
            created_by=principal.actor,
            metadata=labels,
        )
        await self._store.create_session(record)
        await self._audit.control(
            principal,
            tenant_id,
            "session_created",
            {
                "package": record.package,
                "version": record.version,
                "entry": record.entry,
                "metadata": labels,
            },
            session_id=record.session_id,
        )
        return record

    async def get(self, principal: Principal, session_id: str) -> SessionRecord:
        """取会话。别的租户的会话按不存在处理。"""
        record = await self._store.get_session(principal.require_tenant(), session_id)
        if record is None:
            raise NotFoundError(f"会话 {session_id!r} 不存在")
        return record

    async def list_sessions(
        self,
        principal: Principal,
        *,
        status: str | None = None,
        mine: bool = False,
        limit: int = 50,
    ) -> list[SessionRecord]:
        """列出本租户的会话；``mine`` 只看本凭据创建的。"""
        return await self._store.list_sessions(
            principal.require_tenant(),
            status=status,
            created_by=principal.actor if mine else None,
            limit=limit,
        )

    async def _open(self, record: SessionRecord) -> _LiveSession:
        """取得会话的持有权并准备好引擎。同一个会话的并发请求只准备一次。"""
        key = (record.tenant_id, record.session_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            live = self._live.get(key)
            if live is not None:
                return live
            await self._store.acquire_lease(
                record.tenant_id,
                record.session_id,
                owner=self._instance,
                now=self._now(),
                ttl_seconds=self._lease_seconds,
            )
            try:
                live = await self._start_engine(record)
            except BaseException:
                await self._store.release_lease(
                    record.tenant_id, record.session_id, owner=self._instance
                )
                raise
            self._live[key] = live
            return live

    async def _start_engine(self, record: SessionRecord) -> _LiveSession:
        """创建（或从存储里续接）引擎，并接上事件枢纽。"""
        package = await self._packages.resolve(record.tenant_id, record.package, record.version)
        runtime = await self._runtimes.get(package)
        engine = await runtime.pool.get_or_create(
            session_id=record.session_id,
            entry_skill_id=record.entry,
            resume_thread_id=record.thread_id,
        )
        key = (record.tenant_id, record.session_id)

        def turn_ended(submission_id: str) -> None:
            live = self._live.get(key)
            if live is not None:
                live.in_flight.discard(submission_id)
                live.last_used = self._now()

        hub = EngineEventHub(
            engine,
            tenant_id=record.tenant_id,
            session_id=record.session_id,
            audit=self._audit,
            store=self._store,
            capture_requests=self._runtimes.defaults.capture_requests,
            on_turn_end=turn_ended,
        )
        await hub.start()
        self._submissions.register_thread(engine.thread_id, record.session_id)
        if record.thread_id != engine.thread_id:
            record = await self._store.update_session(
                record.tenant_id, record.session_id, at=self._now(), thread_id=engine.thread_id
            )
        return _LiveSession(
            record=record, runtime=runtime, engine=engine, hub=hub, last_used=self._now()
        )

    async def _active(self, principal: Principal, session_id: str) -> _LiveSession:
        """取一个可以继续对话的会话。"""
        record = await self.get(principal, session_id)
        if record.status != "active":
            raise ConflictError(f"会话 {session_id!r} 已关闭")
        return await self._open(record)

    def in_flight_turns(self, tenant_id: str) -> int:
        """本实例上该租户进行中的对话数。"""
        return sum(len(live.in_flight) for key, live in self._live.items() if key[0] == tenant_id)

    async def _admit_turn(self, tenant_id: str) -> None:
        """开始一轮对话前的准入：不在停机中，且未超过租户的并发上限。"""
        if self._draining:
            raise RuntimeUnavailableError("本实例正在停机，请重试其他实例")
        tenant = await self._quota.active_tenant(tenant_id)
        limit = tenant.quota.max_concurrent_turns
        if limit is not None and self.in_flight_turns(tenant_id) >= limit:
            raise QuotaExceededError(f"进行中的对话数已达上限 {limit}，请稍后再试")

    async def _submit(self, live: _LiveSession, op: Any) -> Turn:
        """提交一个操作并登记它的事件流。"""
        submission_id = await live.engine.submit(op)
        # 提交与登记之间不能有让出点，否则内核先发出的事件会漏掉
        stream = live.hub.open_stream(submission_id)
        live.in_flight.add(submission_id)
        live.last_used = self._now()
        self._submissions.register(submission_id, live.record.session_id)
        await self._store.update_session(
            live.record.tenant_id, live.record.session_id, at=self._now()
        )
        return Turn(live.record.session_id, submission_id, stream)

    async def send_message(self, principal: Principal, session_id: str, text: str) -> Turn:
        """发一条用户消息，开始一轮对话。"""
        if not text.strip():
            raise InvalidRequestError("消息不能为空")
        if len(text) > MAX_MESSAGE_CHARS:
            raise InvalidRequestError(f"消息超过 {MAX_MESSAGE_CHARS} 个字符")
        await self._quota.check_new_turn(principal.require_tenant())
        await self._admit_turn(principal.require_tenant())
        live = await self._active(principal, session_id)
        turn = await self._submit(live, taifeng.UserMessage(text=text))
        # 用户说了什么不在内核事件里，审计要有它才完整（超长部分会被截断）
        await self._audit.control(
            principal,
            live.record.tenant_id,
            "message_sent",
            {"submission_id": turn.submission_id, "text": text},
            session_id=session_id,
        )
        return turn

    async def resume(
        self, principal: Principal, session_id: str, resolutions: dict[str, Any]
    ) -> Turn:
        """送回审批结果（或表单答案），让挂起的那一轮对话续跑。"""
        if not resolutions:
            raise InvalidRequestError("resolutions 不能为空")
        await self._admit_turn(principal.require_tenant())
        live = await self._active(principal, session_id)
        turn = await self._submit(
            live, taifeng.Resume(thread_id=live.engine.thread_id, resolutions=resolutions)
        )
        await self._audit.control(
            principal,
            live.record.tenant_id,
            "suspension_resolved_by",
            {
                "submission_id": turn.submission_id,
                "decisions": {
                    request_id: value.get("granted") if isinstance(value, dict) else None
                    for request_id, value in resolutions.items()
                },
            },
            session_id=session_id,
        )
        return turn

    async def pending(self, principal: Principal, session_id: str) -> list[dict[str, Any]]:
        """会话当前等待处理的审批或表单请求。

        以审计为准（各实例共享）：最近一次挂起里的请求，减去之后已经核销或到期的。
        """
        record = await self.get(principal, session_id)
        query = AuditQuery(session_id=session_id, source="engine", limit=1000)
        suspended: AuditRecord | None = None
        settled: set[str] = set()
        after = 0
        while True:
            page = await self._store.query_audit(record.tenant_id, query)
            for item in page:
                if item.kind == "turn_suspended":
                    suspended, settled = item, set()
                elif suspended is not None and item.kind in (
                    "suspension_resolved",
                    "suspension_partially_resolved",
                    "suspension_expired",
                ):
                    if item.data.get("record_id") == suspended.data.get("record_id"):
                        resolved = item.data.get("resolved_request_ids") or item.data.get(
                            "request_ids"
                        )
                        if item.kind == "suspension_expired" or not isinstance(resolved, list):
                            suspended = None
                        else:
                            settled.update(str(value) for value in resolved)
            if len(page) < query.limit:
                break
            after = page[-1].audit_id
            query = AuditQuery(session_id=session_id, source="engine", limit=1000, after_id=after)
        if suspended is None:
            return []
        pending = suspended.data.get("pending")
        if not isinstance(pending, list):
            return []
        return [
            dict(item)
            for item in pending
            if isinstance(item, dict) and str(item.get("request_id")) not in settled
        ]

    async def replay(
        self, principal: Principal, session_id: str, submission_id: str, *, after_seq: int = -1
    ) -> list[dict[str, Any]]:
        """回放一轮对话已经记入审计的事件（流式增量除外；回复全文在 ``turn_text`` 里）。"""
        record = await self.get(principal, session_id)
        events: list[dict[str, Any]] = []
        after = 0
        while True:
            page = await self._store.query_audit(
                record.tenant_id,
                AuditQuery(
                    session_id=session_id,
                    submission_id=submission_id,
                    source="engine",
                    after_id=after,
                    limit=1000,
                ),
            )
            for item in page:
                if item.seq is not None and item.seq <= after_seq:
                    continue
                data = {key: value for key, value in item.data.items() if key != "submission_id"}
                events.append({"kind": item.kind, "seq": item.seq, "at": item.at, "data": data})
            if len(page) < 1000:
                return events
            after = page[-1].audit_id

    async def _detach(self, key: tuple[str, str], *, force: bool) -> bool:
        """释放本实例对会话的持有：关引擎、写完审计、放弃租约。

        ``force=False`` 时内核会因为还有后台子任务而拒绝释放引擎；这种情况下什么都不动，
        返回 False，下一轮再试。
        """
        live = self._live.get(key)
        if live is None:
            return False
        await live.runtime.pool.release(live.record.session_id, force=force)
        if live.record.session_id in live.runtime.pool.introspect():
            return False
        self._live.pop(key, None)
        try:
            await live.hub.close()
        finally:
            await self._store.release_lease(key[0], key[1], owner=self._instance)
        return True

    async def release_idle(self) -> list[str]:
        """释放空闲超过 ``session_idle_seconds`` 的会话的引擎，返回释放了哪些会话。

        会话本身保持活动；下次对话时从存储续接。内存里不再为每个历史会话常驻一个引擎，
        这是并发承载量的前提。
        """
        deadline = self._now() - self._runtimes.defaults.session_idle_seconds
        released: list[str] = []
        for key, live in list(self._live.items()):
            if live.idle and live.last_used <= deadline:
                lock = self._locks.setdefault(key, asyncio.Lock())
                async with lock:
                    current = self._live.get(key)
                    if current is live and live.idle and await self._detach(key, force=False):
                        released.append(key[1])
        return released

    async def maintain(self) -> None:
        """一次维护：续期、释放空闲会话、回收空闲运行时。"""
        await self._renew_leases_once()
        await self.release_idle()
        await self._runtimes.evict_idle()

    async def close(self, principal: Principal, session_id: str) -> SessionRecord:
        """关闭会话。正在进行的对话会被取消。关闭后不能再发消息。"""
        record = await self.get(principal, session_id)
        if record.status == "closed":
            return record
        await self._detach((record.tenant_id, session_id), force=True)
        closed = await self._store.update_session(
            record.tenant_id, session_id, at=self._now(), status="closed"
        )
        await self._audit.control(
            principal, record.tenant_id, "session_closed", session_id=session_id
        )
        return closed

    async def _renew_leases_once(self) -> None:
        """给本实例持有的会话续期。续期失败说明持有权已经丢了，立即放手。"""
        for key in list(self._live):
            try:
                await self._store.acquire_lease(
                    key[0],
                    key[1],
                    owner=self._instance,
                    now=self._now(),
                    ttl_seconds=self._lease_seconds,
                )
            except Exception:
                logger.exception("会话 %s 续期失败，本实例放弃持有", key[1])
                with contextlib.suppress(Exception):
                    await self._detach(key, force=True)

    async def _renew_leases(self) -> None:
        """定期维护：续期、释放空闲会话、回收空闲运行时。"""
        while True:
            await asyncio.sleep(self._lease_seconds / 3)
            try:
                await self.maintain()
            except Exception:
                logger.exception("会话维护失败")

    async def drain(self, timeout_seconds: float) -> int:
        """停止接受新的对话，等在途对话结束（最多等这么久），返回到期时仍在途的对话数。"""
        self._draining = True
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            remaining = sum(len(live.in_flight) for live in self._live.values())
            if remaining == 0:
                return 0
            await asyncio.sleep(0.1)
        return sum(len(live.in_flight) for live in self._live.values())

    async def shutdown(self, *, drain_seconds: float = 0.0) -> None:
        """停止续期，释放本实例持有的全部会话。会话本身保持活动状态，可由别的实例接手。

        Args:
            drain_seconds: 释放之前给在途对话多少时间跑完；0 表示立即释放（在途对话被取消）。
        """
        if drain_seconds > 0:
            remaining = await self.drain(drain_seconds)
            if remaining:
                logger.warning(
                    "停机等待 %.0f 秒后仍有 %d 轮对话在途，将被取消", drain_seconds, remaining
                )
        self._draining = True
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat
            self._heartbeat = None
        for key in list(self._live):
            try:
                await self._detach(key, force=True)
            except Exception:
                logger.exception("释放会话 %s 失败", key[1])


__all__ = ["MAX_MESSAGE_CHARS", "SessionManager", "Turn"]

"""会话管理：建会话、发消息、取事件流、送回审批结果、关闭。

一个会话对应内核里的一个线程，由某一个平台实例持有（单写者）。持有权是带期限的租约：
持有者定期续期；持有者崩溃后租约到期，别的实例才能接手，并从会话存储里续接。
会话存储放在哪里由状态面决定：postgres 状态面下在库里，各实例各用各的数据目录；
local 状态面下是数据目录里的本机文件，跨实例接手就得共用数据目录。

准备引擎（解包、组装运行时、续接历史）期间没人续期，所以续接之前、放进本实例之前
各确认一次持有权，并改用确认时拿到的最新会话记录：这期间别的实例可能接手过（建了线程、
写过几轮）或关闭了会话。确认失败就丢掉刚建的引擎并上抛，不在别人的租约下写，也不拿
过时的记录续接。租约中途到期过（哪怕又续上了）同样算失败，见 ``leases`` 模块。

已知限制：

- 续期间隔受维护耗时影响。续期与释放空闲会话、回收空闲运行时在同一个维护循环里
  串行执行，实际间隔是「租约时长的三分之一 + 一次维护的耗时」；维护卡住（关闭运行时
  很慢、控制面调用迟迟不返回）超过「期限减续期间隔」时，租约可能在持有者还在写时到期。
- 租约期限按各实例自己的墙钟判断（写入与比较用的都是调用方给的时间），依赖 NTP：
  两个实例的时钟偏差超过「期限减续期间隔」时，后来者可能在原持有者还在续期时拿到租约，
  直到原持有者下一次续期失败放手为止，两边可能短暂同时写。
- 存储层没有 fencing：会话存储不核对租约，单写者完全靠持有者及时察觉、及时停写。
- 停止生成不拿会话锁：会话正在准备引擎（还没放进本实例）时取消返回 ``[]``，随后开始的那一轮
  不受影响。不拿锁的前提是非审计模式下 ``submit`` 与登记在途之间不让出（见 ``_submit``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import taifeng

from xuanpu_server.controlplane.models import SessionRecord
from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    LeaseInterruptedError,
    NotFoundError,
    QuotaExceededError,
    RuntimeUnavailableError,
)
from xuanpu_server.ids import new_id
from xuanpu_server.runtime.cancellation import cancel_in_flight, pick_targets
from xuanpu_server.runtime.events import EngineEventHub, SubmissionIndex, TurnStream
from xuanpu_server.runtime.leases import SessionKey, SessionLeases
from xuanpu_server.runtime.session_history import pending_requests, replay_turn

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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


def _require_active(record: SessionRecord) -> None:
    """会话已关闭就不能再开引擎、再对话。"""
    if record.status != "active":
        raise ConflictError(f"会话 {record.session_id!r} 已关闭")


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
        self._leases = SessionLeases(
            store, instance_id=instance_id, lease_seconds=lease_seconds, now=now
        )
        self._now = now
        self._live: dict[SessionKey, _LiveSession] = {}
        self._locks: dict[SessionKey, asyncio.Lock] = {}
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
        async with self._lock_for(key):
            live = self._live.get(key)
            if live is not None:
                return live
            held = await self._leases.hold(record)
            try:
                live = await self._start_engine(held)
            except BaseException:
                await self._let_go(held)
                raise
            try:
                # 准备引擎期间没人续期，租约可能已经过期、被别人拿走：放进来之前再确认一次
                live.record = await self._bind_thread(live, await self._leases.confirm(live.record))
            except BaseException:
                await self._retire(live)
                raise
            self._live[key] = live
            return live

    def _lock_for(self, key: SessionKey) -> asyncio.Lock:
        """一个会话的锁：准备引擎、放进或移出本实例，同一会话同一时刻只做一件。"""
        return self._locks.setdefault(key, asyncio.Lock())

    async def _bind_thread(self, live: _LiveSession, fresh: SessionRecord) -> SessionRecord:
        """确认持有之后，才把引擎的线程登记到会话上（避免拿过期租约改写别人的线程）。

        Args:
            live: 刚准备好的会话。
            fresh: 刚才确认持有时拿到的最新会话记录。
        """
        _require_active(fresh)
        thread_id = live.engine.thread_id
        if fresh.thread_id not in (None, thread_id):
            raise LeaseInterruptedError(f"会话 {fresh.session_id!r} 在准备期间换了线程，请重试")
        if fresh.thread_id is None:
            fresh = await self._store.update_session(
                fresh.tenant_id, fresh.session_id, at=self._now(), thread_id=thread_id
            )
        self._submissions.register_thread(thread_id, fresh.session_id)
        return fresh

    async def _retire(self, live: _LiveSession) -> None:
        """收尾一个已经不在本实例里接单的会话：放掉引擎、关事件枢纽、放弃租约。

        尽力而为：某一步失败只记日志、接着做下一步，不抛出——调用方要么正在处理让它被
        丢掉的那个异常，要么已经认定持有权丢了，收尾失败不改变这一结论（租约到期自然失效）。
        """
        record = live.record
        steps: list[tuple[str, Callable[[], Awaitable[object]]]] = [
            ("引擎", lambda: live.runtime.pool.release(record.session_id, force=True)),
            ("事件枢纽", live.hub.close),
            ("租约", lambda: self._leases.release(record)),
        ]
        for name, step in steps:
            try:
                await step()
            except Exception:
                logger.exception("收尾会话 %s 时%s没有关好", record.session_id, name)

    async def _let_go(self, record: SessionRecord) -> None:
        """放弃租约。失败只记日志（租约到期自然失效），不盖住调用方正在处理的异常。"""
        try:
            await self._leases.release(record)
        except Exception:
            logger.exception("放弃会话 %s 的租约失败，等它到期", record.session_id)

    async def _start_engine(self, held: SessionRecord) -> _LiveSession:
        """创建（或从存储里续接）引擎，并接上事件枢纽。线程在确认持有之后才登记到会话上。

        Args:
            held: 取得持有权时拿到的会话记录。
        """
        package = await self._packages.resolve(held.tenant_id, held.package, held.version)
        runtime = await self._runtimes.get(package)
        # 组装运行时可能比租约还久：续接（可能回填悬空的工具调用、写进存储）之前先确认持有，
        # 并改用最新记录——这期间别的实例可能接手过（建了线程）或关闭了会话
        record = await self._leases.confirm(held)
        _require_active(record)
        engine = await runtime.pool.get_or_create(
            session_id=record.session_id,
            entry_skill_id=record.entry,
            resume_thread_id=record.thread_id,
        )
        key = (record.tenant_id, record.session_id)

        def turn_ended(submission_id: str) -> None:
            """一轮结束：不再算在途，并把这一刻记为最近活动。"""
            live = self._live.get(key)
            if live is not None:
                live.in_flight.discard(submission_id)
                live.last_used = self._now()

        try:
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
        except BaseException:
            # 引擎已经在池里了：接不上事件枢纽就把它放掉，免得留一个没人管的引擎
            try:
                await runtime.pool.release(record.session_id, force=True)
            except Exception:
                logger.exception("释放会话 %s 的引擎失败", record.session_id)
            raise
        return _LiveSession(
            record=record, runtime=runtime, engine=engine, hub=hub, last_used=self._now()
        )

    async def _active(self, principal: Principal, session_id: str) -> _LiveSession:
        """取一个可以继续对话的会话。"""
        record = await self.get(principal, session_id)
        _require_active(record)
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

    async def cancel(
        self, principal: Principal, session_id: str, submission_id: str | None = None
    ) -> list[str]:
        """停止生成：取消会话里在途的对话，返回提交了取消的 submission。

        只管在途、且内核已受理的对话（为什么要等受理见 ``cancellation`` 模块）。挂起等审批的
        那一轮已不在途，拒绝仍走 ``resume``。本实例没持有会话时不为了取消去打开引擎：别的
        实例持有着就抛 ``SessionBusyError``（带持有者），否则这里没有在途对话。

        Args:
            submission_id: 只停这一轮；省略则停全部在途的。给了但不在途抛
                ``InvalidRequestError``。
        """
        record = await self.get(principal, session_id)
        live = self._live.get((record.tenant_id, session_id))
        if live is None:
            self._leases.ensure_not_held_elsewhere(record)
            # 没有在途对话：没指定就是空列表，指定了就是它不在途
            return pick_targets((), submission_id)
        targets = await cancel_in_flight(live.engine, live.in_flight, submission_id)
        if targets:
            await self._audit.control(
                principal,
                record.tenant_id,
                "turn_cancel_requested",
                {"submission_ids": targets},
                session_id=session_id,
            )
        return targets

    async def pending(self, principal: Principal, session_id: str) -> list[dict[str, Any]]:
        """会话当前等待处理的审批或表单请求。

        以审计为准（各实例共享）：最近一次挂起里的请求，减去之后已经核销或到期的。
        """
        record = await self.get(principal, session_id)
        return await pending_requests(self._store, record.tenant_id, session_id)

    async def replay(
        self, principal: Principal, session_id: str, submission_id: str, *, after_seq: int = -1
    ) -> list[dict[str, Any]]:
        """回放一轮对话已经记入审计的事件（流式增量除外；回复全文在 ``turn_text`` 里）。"""
        record = await self.get(principal, session_id)
        return await replay_turn(
            self._store, record.tenant_id, session_id, submission_id, after_seq=after_seq
        )

    async def _detach(self, key: SessionKey, *, force: bool) -> bool:
        """释放本实例对会话的持有：关引擎、写完审计、放弃租约。调用方须持有该会话的锁。

        ``force=False``（空闲释放）先核对、再移除：内核因为还有后台子任务而拒绝释放引擎时
        什么都不动，返回 False，下一轮再试。``force=True``（持有权丢了、关闭会话、停机）
        先从本实例移除、立即停止接单，再尽力收尾，收尾失败只记日志。
        """
        live = self._live.get(key)
        if live is None:
            return False
        if not force:
            await live.runtime.pool.release(live.record.session_id, force=False)
            if live.record.session_id in live.runtime.pool.introspect():
                return False
        del self._live[key]
        await self._retire(live)
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
                async with self._lock_for(key):
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
        key = (record.tenant_id, session_id)
        async with self._lock_for(key):
            await self._detach(key, force=True)
        closed = await self._store.update_session(
            record.tenant_id, session_id, at=self._now(), status="closed"
        )
        await self._audit.control(
            principal, record.tenant_id, "session_closed", session_id=session_id
        )
        return closed

    async def _renew_leases_once(self) -> None:
        """给本实例持有的会话续期。续期失败说明持有权已经丢了，立即放手（续期失败已记日志）。"""
        for key in await self._leases.renew(list(self._live)):
            async with self._lock_for(key):
                await self._detach(key, force=True)

    async def _renew_leases(self) -> None:
        """定期维护：续期、释放空闲会话、回收空闲运行时。"""
        while True:
            await asyncio.sleep(self._leases.lease_seconds / 3)
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
            # 收尾是尽力而为，失败已在里面记日志；一个会话收不干净不影响释放其他会话
            async with self._lock_for(key):
                await self._detach(key, force=True)


__all__ = ["MAX_MESSAGE_CHARS", "SessionManager", "Turn"]

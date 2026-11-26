"""管家事件驱动唤醒总线与条件超时器（ADR 0024）。

- ``WakeBus`` 负责把宿主状态变化（派工结束、任务终态/等待、编组推进、待处理项答复）
  以极小元数据信号推送给 ``Wakeup`` 消费者，避免纯靠高频轮询感知结果。
- ``MemoryWakeBus`` 用于单机/内存模式，按 ``(tenant_id, kind, target_id)`` 合并去重。
- ``PostgresWakeBus`` 在本地合并队列之上叠加 PostgreSQL ``LISTEN/NOTIFY``，支持多实例
  毫秒级唤醒；连接异常时自动退避重连，由低频 Reconciler 兜底期间漏掉的通知。
- ``ConditionalTimerRegistry`` 用于绑定在事件条件上的超时器：目标事件提前完成时自动
  失效取消，到期未完成才触发回调，不靠 ``asyncio.sleep`` 占住协程。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

WakeKind = Literal["dispatch", "task", "group", "pending", "proactive"]
_VALID_KINDS: frozenset[str] = frozenset({"dispatch", "task", "group", "pending", "proactive"})
_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
DEFAULT_CHANNEL = "butler_wake"
MAX_SIGNAL_BYTES = 512


@dataclass(frozen=True)
class WakeSignal:
    """单条唤醒信号；严格只含路由元数据，不携带任何正文。"""

    tenant_id: str
    kind: WakeKind
    target_id: str

    def __post_init__(self) -> None:
        """校验字段格式，防止把自由文本塞进事件通道。"""
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"不支持的唤醒信号类型：{self.kind!r}")
        if not _ID_RE.fullmatch(self.tenant_id):
            raise ValueError(f"无效租户标识：{self.tenant_id!r}")
        if not _ID_RE.fullmatch(self.target_id):
            raise ValueError(f"无效目标标识：{self.target_id!r}")

    @property
    def key(self) -> tuple[str, str, str]:
        """去重合并键。"""
        return (self.tenant_id, self.kind, self.target_id)


def encode_signal(signal: WakeSignal) -> str:
    """将信号编码为紧凑 JSON 字符串（供 PG NOTIFY 通道投递）。"""
    payload = json.dumps(
        {"t": signal.tenant_id, "k": signal.kind, "id": signal.target_id},
        separators=(",", ":"),
        ensure_ascii=True,
    )
    if len(payload.encode("utf-8")) > MAX_SIGNAL_BYTES:
        raise ValueError("唤醒信号超过长度上限")
    return payload


def decode_signal(raw: str) -> WakeSignal | None:
    """解析通道收到的 JSON 信号；畸形负载记日志并忽略，不崩溃监听协程。"""
    if not raw or len(raw.encode("utf-8")) > MAX_SIGNAL_BYTES:
        logger.warning("忽略超长或空的唤醒信号")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("忽略非法 JSON 唤醒信号")
        return None
    if not isinstance(data, dict):
        logger.warning("忽略非对象唤醒信号")
        return None
    tenant, kind, target = data.get("t"), data.get("k"), data.get("id")
    if not (isinstance(tenant, str) and isinstance(kind, str) and isinstance(target, str)):
        logger.warning("忽略字段缺失的唤醒信号")
        return None
    try:
        return WakeSignal(tenant_id=tenant, kind=kind, target_id=target)  # type: ignore[arg-type]
    except ValueError:
        logger.warning("忽略格式不合法的唤醒信号")
        return None


class WakeBus(Protocol):
    """事件驱动唤醒总线契约。"""

    async def start(self) -> None:
        """启动监听（内存实现为空操作）。"""
        ...

    async def close(self) -> None:
        """停止监听并释放资源。"""
        ...

    async def publish(self, signal: WakeSignal) -> None:
        """发布一条唤醒信号（调用方应在权威状态提交后调用）。"""
        ...

    async def drain(self, timeout: float) -> tuple[WakeSignal, ...]:
        """等待并取出当前已合并的全部待处理信号；超时无信号返回空元组。"""
        ...


class MemoryWakeBus:
    """进程内合并去重唤醒总线。"""

    def __init__(self, *, max_pending: int = 4096) -> None:
        """初始化待处理队列与唤醒事件。"""
        self._max_pending = max_pending
        self._pending: dict[tuple[str, str, str], WakeSignal] = {}
        self._ready = asyncio.Event()

    async def start(self) -> None:
        """进程内总线无需额外启动动作。"""

    async def close(self) -> None:
        """清空残留队列并唤醒等待方以便退出。"""
        self._pending.clear()
        self._ready.set()

    def push_nowait(self, signal: WakeSignal) -> None:
        """同步写入并按路由键去重合并（供 PG 监听回调调用）。"""
        if signal.key in self._pending:
            return
        if len(self._pending) >= self._max_pending:
            # 保护内存上界：丢弃最旧条目，剩余状态由低频 Reconciler 兜底
            oldest = next(iter(self._pending))
            del self._pending[oldest]
        self._pending[signal.key] = signal
        self._ready.set()

    async def publish(self, signal: WakeSignal) -> None:
        """入队并立即唤醒消费者。"""
        self.push_nowait(signal)

    async def drain(self, timeout: float) -> tuple[WakeSignal, ...]:
        """若已有信号立即整批取出，否则最多阻塞 ``timeout`` 秒。"""
        if not self._pending and timeout > 0:
            self._ready.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        if not self._pending:
            return ()
        batch = tuple(self._pending.values())
        self._pending.clear()
        self._ready.clear()
        return batch


class PostgresWakeBus:
    """基于 PostgreSQL ``LISTEN/NOTIFY`` 的多实例唤醒总线。"""

    def __init__(
        self,
        pool: Any,
        *,
        channel: str = DEFAULT_CHANNEL,
        reconnect_seconds: float = 2.0,
    ) -> None:
        """复用控制面 asyncpg 连接池；通道名须为受控标识符。"""
        if not _CHANNEL_RE.fullmatch(channel):
            raise ValueError(f"无效 PG NOTIFY 通道名：{channel!r}")
        self._pool = pool
        self._channel = channel
        self._reconnect = reconnect_seconds
        self._local = MemoryWakeBus()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        """启动后台 LISTEN 保活协程。"""
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.ensure_future(self._listen_loop())

    async def close(self) -> None:
        """停止 LISTEN 协程并关闭本地缓冲。"""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._local.close()

    async def publish(self, signal: WakeSignal) -> None:
        """先推本地快路径，再通过 ``pg_notify`` 广播给其余实例。"""
        self._local.push_nowait(signal)
        payload = encode_signal(signal)
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT pg_notify($1, $2)", self._channel, payload)
        except Exception:
            # 广播失败不打断业务提交：本实例已入队，其他实例由低频 Reconciler 兜底
            logger.exception("发送 PG NOTIFY 唤醒信号失败（由对账兜底）")

    async def publish_in_tx(self, conn: Any, signal: WakeSignal) -> None:
        """在调用方事务内执行 ``pg_notify``，保证仅在 COMMIT 成功后投递。"""
        payload = encode_signal(signal)
        await conn.execute("SELECT pg_notify($1, $2)", self._channel, payload)

    async def drain(self, timeout: float) -> tuple[WakeSignal, ...]:
        """从本地合并队列取出本批信号。"""
        return await self._local.drain(timeout)

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        """asyncpg 通知回调：解码后写入本地合并队列。"""
        signal = decode_signal(payload)
        if signal is not None:
            self._local.push_nowait(signal)

    async def _listen_loop(self) -> None:
        """独占一条连接监听通道；断线后退避重连。"""
        while not self._stopping:
            conn: Any = None
            try:
                conn = await self._pool.acquire()
                await conn.add_listener(self._channel, self._on_notify)
                while not self._stopping and not conn.is_closed():
                    await asyncio.sleep(self._reconnect)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("PG LISTEN 唤醒监听断开，%.1f 秒后重连", self._reconnect)
                await asyncio.sleep(self._reconnect)
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.remove_listener(self._channel, self._on_notify)
                    with contextlib.suppress(Exception):
                        await self._pool.release(conn)


@dataclass
class _ArmedTimer:
    """一条已挂起的条件超时器。"""

    key: str
    deadline: float
    callback: Callable[[], Awaitable[None]]


class ConditionalTimerRegistry:
    """条件超时器注册表：关联目标提前完成即取消，到期未取消才触发回调。"""

    def __init__(self) -> None:
        """初始化按业务键索引的超时器集合。"""
        self._timers: dict[str, _ArmedTimer] = {}

    def arm(
        self, key: str, deadline: float, callback: Callable[[], Awaitable[None]],
    ) -> None:
        """登记或覆盖一个条件超时器。"""
        self._timers[key] = _ArmedTimer(key=key, deadline=deadline, callback=callback)

    def cancel(self, key: str) -> bool:
        """目标事件提前完成时取消对应超时器；返回是否命中。"""
        return self._timers.pop(key, None) is not None

    def next_wait(self, now: float, *, max_wait: float) -> float:
        """距最近一条到期超时器还剩多少秒（不超过 ``max_wait``）。"""
        if not self._timers:
            return max_wait
        soonest = min(t.deadline for t in self._timers.values())
        return max(0.0, min(max_wait, soonest - now))

    async def fire_due(self, now: float) -> int:
        """触发所有已到期超时器并移出集合；返回触发条数。"""
        due = [t for t in self._timers.values() if t.deadline <= now]
        for timer in due:
            self._timers.pop(timer.key, None)
        fired = 0
        for timer in due:
            try:
                await timer.callback()
                fired += 1
            except Exception:
                logger.exception("条件超时器 %s 回调执行失败", timer.key)
        return fired

    def __len__(self) -> int:
        """当前挂起的超时器数量。"""
        return len(self._timers)

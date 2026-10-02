"""平台指标：给 ``GET /metrics`` 出 Prometheus 文本格式。

每个 ``Platform`` 一份 ``PlatformMetrics``，各自一个 ``CollectorRegistry``（不用
prometheus_client 的进程级全局登记表）：同一进程里的几个平台各自计数，互不影响。

指标不带租户、会话、业务包之类的标签：这些取值随业务增长，做标签会让序列数跟着涨，拖垮
抓取方；按租户看用量走 ``/v1/usage``。标签只取代码里固定的几个值，已知的取值在构造时就
登记为 0，没发生过的组合也有序列，``rate()`` 不会因为序列缺失而断档。

为什么不用 prometheus_client 的 ``Counter``：它给每个计数序列另出一条 ``_created``，关掉只能
改进程级的全局开关（会波及同进程里别的使用者）。这里计数记在本对象里，由一个自定义收集器在
抓取时读出（prometheus_client 的标准用法）；三个 gauge 也在抓取时经回调现取当前值。

打点的地方都经构造参数拿到本对象或它的方法（审计写入口、事件枢纽、拒绝补记、飞书登录），没有
全局单例。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector
from xuanpu_spec.manifest import Transport

from xuanpu_server.identity.models import LoginFailureReason

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from prometheus_client.core import Metric

TURN_OUTCOMES: tuple[str, ...] = (
    "completed",
    "failed",
    "suspended",
    "cancelled",
    "resume_rejected",
    "resume_partial",
)
"""``xuanpu_turns_total`` 的 ``outcome``。

后两个是送回审批没有续跑的那一次（被拒、只核销了一部分），不是一轮对话，用量里也不计一轮。
"""

GATE_DECISIONS: tuple[str, ...] = ("allowed", "denied", "rejected")
"""``xuanpu_connector_calls_total`` 的 ``decision``，与过闸记录的 ``decision`` 一致。"""

CONNECTOR_TRANSPORTS: tuple[str, ...] = get_args(Transport)
"""``xuanpu_connector_calls_total`` 的 ``transport``：业务包规范里的三种传输。"""

BACKFILL_STAGES: tuple[str, ...] = ("incomplete", "write", "unconfirmed")
"""``xuanpu_gate_backfill_failures_total`` 的 ``stage``：待办缺调用信息、写不进记录、等不到内核
对那次送回的结论。读不到待办时整次送回失败、没有提交给内核，不算补记失败；送回在内核里排队时
被停下（内核没处理它，待办还在）也不算。"""

LOGIN_RESULTS: tuple[str, ...] = ("succeeded", *get_args(LoginFailureReason))
"""``xuanpu_logins_total`` 的 ``result``：登录成功，或七种失败原因之一（设计 7.1）。"""


class PlatformMetrics:
    """一个平台的指标。

    Attributes:
        registry: 本平台专用的登记表，``render`` 从它出文本。
    """

    def __init__(
        self,
        *,
        sessions_live: Callable[[], int],
        turns_in_flight: Callable[[], int],
        runtimes_live: Callable[[], int],
    ) -> None:
        """
        Args:
            sessions_live: 取本实例当前持有的会话数（有引擎的会话）。
            turns_in_flight: 取本实例当前进行中的对话数。
            runtimes_live: 取本实例当前组装着的运行时数。

        三个回调在抓取时才调用，所以可以引用构造本对象时还没建好的部件。
        """
        self._gauges = (
            ("xuanpu_sessions_live", "本实例当前持有的会话数（有引擎的会话）", sessions_live),
            ("xuanpu_turns_in_flight", "本实例当前进行中的对话数", turns_in_flight),
            ("xuanpu_runtimes_live", "本实例当前组装着的运行时数", runtimes_live),
        )
        self._turns = dict.fromkeys(TURN_OUTCOMES, 0)
        self._connector_calls: dict[tuple[str, ...], int] = {
            (transport, decision): 0
            for transport in CONNECTOR_TRANSPORTS
            for decision in GATE_DECISIONS
        }
        self._backfill_failures = dict.fromkeys(BACKFILL_STAGES, 0)
        self._logins = dict.fromkeys(LOGIN_RESULTS, 0)
        self._audit_write_failures = 0
        self._usage_estimated = 0
        self._usage_write_failures = 0
        self.registry = CollectorRegistry()
        self.registry.register(_PlatformCollector(self))

    def turn_ended(self, outcome: str) -> None:
        """一轮对话（或送回审批没有续跑的那一次）结束。"""
        self._turns[outcome] = self._turns.get(outcome, 0) + 1

    def connector_call(self, transport: str, decision: str) -> None:
        """记下一条过闸记录：闸门的放行与拒绝，以及人工拒绝的补记。"""
        key = (transport, decision)
        self._connector_calls[key] = self._connector_calls.get(key, 0) + 1

    def audit_write_failed(self) -> None:
        """一次审计写入失败（引擎事件成批写，每次失败的尝试算一次）。"""
        self._audit_write_failures += 1

    def backfill_failed(self, stage: str) -> None:
        """人工拒绝的过闸记录没补上。``stage`` 取 ``BACKFILL_STAGES`` 里的值。"""
        self._backfill_failures[stage] = self._backfill_failures.get(stage, 0) + 1

    def usage_estimated(self) -> None:
        """被停下的一轮按上下文估算入账了一次（审计里的 ``usage_estimated``）。"""
        self._usage_estimated += 1

    def usage_write_failed(self) -> None:
        """一轮对话结束时用量没有入账（写控制面失败；这一轮的用量不再补记）。"""
        self._usage_write_failures += 1

    def login(self, result: str) -> None:
        """一次飞书登录有了结果。``result`` 取 ``LOGIN_RESULTS`` 里的值。"""
        self._logins[result] = self._logins.get(result, 0) + 1

    def render(self) -> bytes:
        """Prometheus 文本格式（0.0.4）。"""
        return generate_latest(self.registry)

    def families(self) -> Iterator[Metric]:
        """当前的全部指标。gauge 在这里调回调现取。"""
        for name, documentation, current in self._gauges:
            yield GaugeMetricFamily(name, documentation, value=current())
        yield _counter(
            "xuanpu_turns", "结束的对话轮数，按结局分", ["outcome"],
            {(outcome,): count for outcome, count in self._turns.items()},
        )  # fmt: skip
        yield _counter(
            "xuanpu_connector_calls", "过闸记录数（连接器调用），按传输与决定分",
            ["transport", "decision"], self._connector_calls,
        )  # fmt: skip
        yield _counter(
            "xuanpu_audit_write_failures", "审计写入失败的次数", [],
            {(): self._audit_write_failures},
        )  # fmt: skip
        yield _counter(
            "xuanpu_gate_backfill_failures", "人工拒绝的过闸记录没补上的次数，按环节分", ["stage"],
            {(stage,): count for stage, count in self._backfill_failures.items()},
        )  # fmt: skip
        yield _counter(
            "xuanpu_usage_estimated", "被停下的一轮按上下文估算入账的次数", [],
            {(): self._usage_estimated},
        )  # fmt: skip
        yield _counter(
            "xuanpu_usage_write_failures", "一轮对话结束时用量入账失败的次数", [],
            {(): self._usage_write_failures},
        )  # fmt: skip
        yield _counter(
            "xuanpu_logins", "飞书登录的结果：成功，或失败原因", ["result"],
            {(result,): count for result, count in self._logins.items()},
        )  # fmt: skip


def _counter(
    name: str, documentation: str, labels: list[str], values: dict[tuple[str, ...], int]
) -> CounterMetricFamily:
    """一个计数指标族；``values`` 是「标签取值 → 计数」。"""
    family = CounterMetricFamily(name, documentation, labels=labels)
    for label_values, count in values.items():
        family.add_metric(list(label_values), count)
    return family


class _PlatformCollector(Collector):
    """把 ``PlatformMetrics`` 接到登记表上的收集器：抓取时现取。"""

    def __init__(self, metrics: PlatformMetrics) -> None:
        """绑定指标。"""
        self._metrics = metrics

    def collect(self) -> Iterator[Metric]:
        """登记表抓取时调用。"""
        return self._metrics.families()


__all__ = [
    "BACKFILL_STAGES",
    "CONNECTOR_TRANSPORTS",
    "GATE_DECISIONS",
    "LOGIN_RESULTS",
    "TURN_OUTCOMES",
    "PlatformMetrics",
]

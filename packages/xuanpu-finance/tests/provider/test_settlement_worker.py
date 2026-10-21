"""验证发送前复核和未知结果只查单的后台边界。"""

from __future__ import annotations

from typing import Any

import httpx
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.settlement.worker import SettlementWorker


class Store:
    """只记录持久事务调用顺序的可控替身。"""

    def __init__(self, mode: str = "submit") -> None:
        """构造同一冻结外部请求的工作项。"""
        self.calls: list[Any] = []
        self.job = {
            "id": "f2op_1",
            "tenant_id": "t1",
            "legal_entity_id": "c1",
            "lease_token": "lease",
            "mode": mode,
            "actor_id": "executor",
            "authorization_user_ids": ["executor", "approver"],
            "request": {"external_request_id": "ext1"},
        }

    async def claim_settlement(self, *, allow_dispatch: bool = True) -> dict[str, Any]:
        """记录模式允许范围。"""
        self.calls.append(("claim", allow_dispatch))
        return self.job

    async def renew_settlement(self, job: Any) -> None:
        """测试默认处理时间不需要续租。"""

    async def release_settlement(self, job: Any) -> None:
        """释放只属于当前实例的租约。"""
        self.calls.append("release")

    async def prepare_settlement_dispatch(self, job: Any) -> dict[str, Any]:
        """发送前持久化dispatching事实。"""
        self.calls.append("prepare")
        return job["request"]

    async def record_settlement_result(self, job: Any, result: Any, raw: bytes) -> None:
        """保存原字节和语义结果。"""
        self.calls.append(("result", result, raw))

    async def defer_settlement(self, job: Any, code: str, *, uncertain: bool) -> None:
        """不确定结果保留占用。"""
        self.calls.append(("defer", code, uncertain))

    async def reject_settlement_before_dispatch(self, job: Any, code: str) -> None:
        """只有明确发送前拒绝才可以确定失败。"""
        self.calls.append(("reject", code))


class Providers:
    """模拟有副作用提交和只读查询。"""

    def __init__(self, store: Store, fail: bool = False) -> None:
        """共享顺序日志以证明先持久后发送。"""
        self.store, self.fail = store, fail

    async def submit(self, request: Any) -> tuple[dict[str, Any], bytes]:
        """网络超时视为可能已经发送。"""
        self.store.calls.append("submit")
        if self.fail:
            raise httpx.ReadTimeout("timeout")
        return {"status": "accepted"}, b"accepted"

    async def query(self, request: Any) -> tuple[dict[str, Any], bytes]:
        """未知只能查询同一请求。"""
        self.store.calls.append("query")
        return {"status": "not_found"}, b"not_found"


class Gateway:
    """用于验证全体实际批准人的在线资格。"""

    def __init__(self, store: Store, active: bool = True, failed: bool = False) -> None:
        """允许模拟平台不可用或人员停用。"""
        self.store, self.is_active, self.failed = store, active, failed

    async def active(self, who: Any) -> bool:
        """只从持久身份生成在线查询。"""
        self.store.calls.append(("identity", who.user_id))
        if self.failed:
            raise FinanceError("identity_unavailable", 503, "平台不可用")
        return self.is_active


async def test_worker_persists_dispatch_before_http_and_checks_approvers() -> None:
    """所有批准人员在线核对完成后才置发送事实。"""
    store = Store()
    assert await SettlementWorker(store, Providers(store), Gateway(store)).run_once()
    assert store.calls.index("prepare") < store.calls.index("submit")
    assert ("identity", "approver") in store.calls
    assert store.calls[-1] == "release"


async def test_inactive_authorizer_never_submits() -> None:
    """发送前停用是确定拒绝。"""
    store = Store()
    await SettlementWorker(store, Providers(store), Gateway(store, active=False)).run_once()
    assert "submit" not in store.calls and "prepare" not in store.calls
    assert any(call[0] == "reject" for call in store.calls if isinstance(call, tuple))


async def test_http_timeout_is_unknown_and_never_retried() -> None:
    """提交只调用一次，超时保持未知。"""
    store = Store()
    await SettlementWorker(store, Providers(store, fail=True), Gateway(store)).run_once()
    assert store.calls.count("submit") == 1
    assert any(call[0] == "defer" and call[2] for call in store.calls if isinstance(call, tuple))


async def test_query_survives_identity_outage_and_read_only_mode() -> None:
    """已发查单无需平台在线且不会触发新副作用。"""
    store = Store("query")
    await SettlementWorker(store, Providers(store), Gateway(store, failed=True)).run_once(
        allow_dispatch=False
    )
    assert "query" in store.calls and "submit" not in store.calls
    assert not any(call[0] == "identity" for call in store.calls if isinstance(call, tuple))
    assert ("claim", False) in store.calls


async def test_lost_lease_cancels_in_flight_http() -> None:
    """续租失败取消旧HTTP任务且不能提交旧租约结果。"""
    import asyncio

    store = Store()
    started, cancelled = asyncio.Event(), asyncio.Event()

    class HangingProviders(Providers):
        """维持发送直到租约取消。"""

        async def submit(self, request: Any) -> tuple[dict[str, Any], bytes]:
            """取消不能转换为重发或成功。"""
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("HTTP不应继续")

    class LostWorker(SettlementWorker):
        """在HTTP已开始后模拟数据库fence失效。"""

        async def _heartbeat(self, job: dict[str, Any]) -> None:
            """持久租约丢失立即传播。"""
            await started.wait()
            raise FinanceError("LEASE_LOST", 409, "租约失效")

    assert await LostWorker(store, HangingProviders(store), Gateway(store)).run_once()
    assert cancelled.is_set()
    assert not any(call[0] == "result" for call in store.calls if isinstance(call, tuple))


async def test_unavailable_identity_defers_without_dispatching() -> None:
    """平台暂不可用时保持未发队列，不产生副作用。"""
    store = Store()
    await SettlementWorker(store, Providers(store), Gateway(store, failed=True)).run_once()
    assert "prepare" not in store.calls and "submit" not in store.calls
    assert ("defer", "identity_unavailable", False) in store.calls


async def test_missing_approval_identity_snapshot_fails_closed() -> None:
    """不能因旧工作项缺批准人列表而只核对经办身份。"""
    store = Store()
    del store.job["authorization_user_ids"]
    await SettlementWorker(store, Providers(store), Gateway(store)).run_once()
    assert "submit" not in store.calls and "prepare" not in store.calls
    assert ("reject", "AUTHORIZATION_SNAPSHOT_INVALID") in store.calls


async def test_invalid_receipt_raw_is_preserved_as_unknown() -> None:
    """错误回执使用可信冻结范围保全原文，不采纳外来业务字段。"""
    from xuanpu_finance.settlement.provider import RESULT_SCOPE, ProviderResponseError

    store = Store("query")
    store.job["request"] = {field: "trusted" for field in RESULT_SCOPE}

    class InvalidProviders(Providers):
        """渠道返回跨租户畸形证据。"""

        async def query(self, request: Any) -> tuple[dict[str, Any], bytes]:
            """抛出带原始证据的认证后语义错误。"""
            raise ProviderResponseError(b'{"tenant_id":"other","status":"succeeded"}')

    await SettlementWorker(store, InvalidProviders(store), Gateway(store)).run_once()
    recorded = next(call for call in store.calls if isinstance(call, tuple) and call[0] == "result")
    assert recorded[1]["tenant_id"] == "trusted"
    assert recorded[1]["status"] == "unknown"
    assert recorded[1]["validation_error"] == "PROVIDER_RESPONSE_INVALID"
    assert b'"other"' in recorded[2]


async def test_mode_changes_while_identity_waits_prevent_dispatch_prepare() -> None:
    """人员目录等待期间模块停用，恢复后不得继续进入发送准备。"""
    import asyncio

    store = Store()
    waiting, resume = asyncio.Event(), asyncio.Event()
    state = {"enabled": True}

    class WaitingGateway(Gateway):
        """用屏障确定模式切换发生在身份网络等待期间。"""

        async def active(self, who: Any) -> bool:
            """身份结果本身合法，但不能覆盖后来改变的运行模式。"""
            waiting.set()
            await resume.wait()
            return await super().active(who)

    worker = SettlementWorker(
        store, Providers(store), WaitingGateway(store), dispatch_allowed=lambda: state["enabled"]
    )
    task = asyncio.create_task(worker.run_once())
    await waiting.wait()
    state["enabled"] = False
    resume.set()
    assert await task
    assert "prepare" not in store.calls and "submit" not in store.calls
    assert ("defer", "DISPATCH_PAUSED", False) in store.calls


async def test_mode_changes_after_prepare_preserve_unknown_without_http() -> None:
    """dispatching提交后模式改变，只进入核对且保留金额占用。"""
    state = {"enabled": True}

    class StoppedAfterPrepare(Store):
        """精确模拟数据库提交期间变为只读或失去实例锁。"""

        async def prepare_settlement_dispatch(self, job: Any) -> dict[str, Any]:
            """已经持久化发送事实后关闭新副作用许可。"""
            request = await super().prepare_settlement_dispatch(job)
            state["enabled"] = False
            return request

    store = StoppedAfterPrepare()
    worker = SettlementWorker(
        store, Providers(store), Gateway(store), dispatch_allowed=lambda: state["enabled"]
    )
    assert await worker.run_once()
    assert "prepare" in store.calls and "submit" not in store.calls
    assert ("defer", "DISPATCH_PAUSED", True) in store.calls


async def test_preflight_network_outage_defers_without_stopping_worker_or_sending() -> None:
    """物化证据尚未发送时的网络暂不可用不能停止整个队列。"""
    class PreflightStore(Store):
        """故障发生在dispatching持久边界之前。"""

        async def prepare_settlement_dispatch(self, job: Any) -> dict[str, Any]:
            """模拟实际取证连接超时。"""
            raise httpx.ConnectError("temporary evidence outage")

    store = PreflightStore()
    assert await SettlementWorker(store,Providers(store),Gateway(store)).run_once()
    assert ("defer","PREFLIGHT_NETWORK_UNAVAILABLE",False) in store.calls
    assert "submit" not in store.calls

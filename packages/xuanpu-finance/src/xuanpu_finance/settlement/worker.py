"""独立持久外部执行队列：先记发送事实，未知只能查单。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.settlement.provider import RESULT_SCOPE, ProviderResponseError

if TYPE_CHECKING:
    from collections.abc import Callable


class SettlementStore(Protocol):
    """由独立财务数据库提供原子租约、发送准备和结果提交。"""

    async def claim_settlement(self, *, allow_dispatch: bool = True) -> dict[str, Any] | None:
        """认领工作，过期发送中的工作只能返回query。"""
        ...

    async def renew_settlement(self, job: dict[str, Any]) -> None:
        """续租失败必须抛LEASE_LOST。"""
        ...

    async def release_settlement(self, job: dict[str, Any]) -> None:
        """只释放本租约。"""
        ...

    async def prepare_settlement_dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        """事务复核批准与版本并持久dispatching。"""
        ...

    async def record_settlement_result(
        self, job: dict[str, Any], result: dict[str, Any], raw: bytes
    ) -> None:
        """原字节证据与合法业务状态同事务提交。"""
        ...

    async def defer_settlement(self, job: dict[str, Any], code: str, *, uncertain: bool) -> None:
        """未知不释放金额占用且不能再次提交。"""
        ...

    async def reject_settlement_before_dispatch(self, job: dict[str, Any], code: str) -> None:
        """明确未发送工作才能确定拒绝。"""
        ...


class SettlementProviders(Protocol):
    """只提供单次提交与只读查单的成熟HTTP适配器。"""

    async def submit(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        """发送一次固定请求。"""
        ...

    async def query(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        """只查原请求号。"""
        ...


class IdentityGateway(Protocol):
    """发送前复核真实人员状态。"""

    async def active(self, who: FinancePrincipal) -> bool:
        """在线目录失败不能视为活跃。"""
        ...


def _lease_lost(error: BaseException) -> bool:
    """仅明确租约失效可以正常退让，数据库异常不得隐藏。"""
    if isinstance(error, BaseExceptionGroup):
        return all(_lease_lost(child) for child in error.exceptions)
    return isinstance(error, FinanceError) and error.code == "LEASE_LOST"


class SettlementWorker:
    """不依赖模型生命周期，查单不依赖员工在线。"""

    def __init__(
        self,
        store: SettlementStore,
        providers: SettlementProviders,
        gateway: IdentityGateway,
        *,
        dispatch_allowed: Callable[[], bool] | None = None,
    ) -> None:
        """显式注入独立存储、渠道注册表和平台身份出口。"""
        self.store, self.providers, self.gateway = store, providers, gateway
        self.dispatch_allowed = dispatch_allowed

    def _may_dispatch(self) -> bool:
        """在每个发送边界同步读取当前模式和实例锁状态。"""
        return self.dispatch_allowed is None or self.dispatch_allowed()

    async def _heartbeat(self, job: dict[str, Any]) -> None:
        """续租失败通过任务组立刻取消旧网络工作。"""
        while True:
            await asyncio.sleep(20)
            await self.store.renew_settlement(job)

    async def run_once(self, *, allow_dispatch: bool = True) -> bool:
        """运行一项；停止新发送时继续独立处理在途查询。"""
        job = await self.store.claim_settlement(allow_dispatch=allow_dispatch)
        if job is None:
            return False
        try:
            async with asyncio.TaskGroup() as group:
                heartbeat = group.create_task(self._heartbeat(job))
                try:
                    await self._process(job, allow_dispatch=allow_dispatch)
                finally:
                    heartbeat.cancel()
        except BaseExceptionGroup as error:
            if not _lease_lost(error):
                raise
        finally:
            await self.store.release_settlement(job)
        return True

    async def _authorized(self, job: dict[str, Any]) -> bool:
        """复核经办与所有实际批准人，失败不产生外部请求。"""
        snapshot = job.get("authorization_user_ids")
        if (
            not isinstance(snapshot, list)
            or not snapshot
            or any(not isinstance(user, str) or not user for user in snapshot)
        ):
            await self.store.reject_settlement_before_dispatch(
                job, "AUTHORIZATION_SNAPSHOT_INVALID"
            )
            return False
        users = list(dict.fromkeys([job["actor_id"], *snapshot]))
        for user_id in users:
            try:
                active = await self.gateway.active(FinancePrincipal(job["tenant_id"], user_id))
            except FinanceError as error:
                await self.store.defer_settlement(job, error.code, uncertain=False)
                return False
            if not active:
                await self.store.reject_settlement_before_dispatch(job, "ACCOUNT_INACTIVE")
                return False
        return True

    async def _prepare_submit(
        self, job: dict[str, Any], *, allow_dispatch: bool
    ) -> dict[str, Any] | None:
        """发送前独立核对实时许可与授权，None表示未取得可发送请求。"""
        if not allow_dispatch or not self._may_dispatch():
            await self.store.defer_settlement(job, "DISPATCH_PAUSED", uncertain=False)
            return None
        if not await self._authorized(job):
            return None
        if not self._may_dispatch():
            await self.store.defer_settlement(job, "DISPATCH_PAUSED", uncertain=False)
            return None
        try:
            request = await self.store.prepare_settlement_dispatch(job)
        except httpx.HTTPError:
            await self.store.defer_settlement(job, "PREFLIGHT_NETWORK_UNAVAILABLE", uncertain=False)
            return None
        except FinanceError as error:
            if _lease_lost(error):
                raise
            if error.status >= 500:
                await self.store.defer_settlement(job, error.code, uncertain=False)
            else:
                await self.store.reject_settlement_before_dispatch(job, error.code)
            return None
        if not self._may_dispatch():
            await self.store.defer_settlement(job, "DISPATCH_PAUSED", uncertain=True)
            return None
        return request

    async def _process(self, job: dict[str, Any], *, allow_dispatch: bool) -> None:
        """发送与查单分支互斥，发送后任何模糊结果只进入核对。"""
        request = job["request"]
        submit = job["mode"] == "submit"
        if submit:
            request = await self._prepare_submit(job, allow_dispatch=allow_dispatch)
            if request is None:
                return
        try:
            result, raw = await (
                self.providers.submit(request) if submit else self.providers.query(request)
            )
        except ProviderResponseError as error:
            result = {field: request[field] for field in RESULT_SCOPE}
            result.update(status="unknown", validation_error=error.code)
            if error.readback_evidence:
                result["readback_evidence"] = error.readback_evidence
            await self.store.record_settlement_result(job, result, error.raw)
            return
        except (httpx.HTTPError, FinanceError) as error:
            if _lease_lost(error):
                raise
            code = error.code if isinstance(error, FinanceError) else "PROVIDER_NETWORK_UNKNOWN"
            await self.store.defer_settlement(job, code, uncertain=True)
            return
        except asyncio.CancelledError:
            # dispatching事实已落库；停机或租约失效后只允许查单，持久恢复无需重发。
            raise
        await self.store.record_settlement_result(job, result, raw)

"""把独立F3队列映射到既有可靠租约执行器。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xuanpu_finance.compliance.archive import ArchivePipeline
    from xuanpu_finance.storage import FinanceStore


class ComplianceQueueAdapter:
    """复用发送门禁与心跳，不复制一套后台网络恢复逻辑。"""

    def __init__(self, store: FinanceStore, archive: ArchivePipeline | None = None) -> None:
        """注入独立领域存储和事务外材料物化器。"""
        self.store, self.archive = store, archive

    async def claim_settlement(self, *, allow_dispatch: bool = True) -> dict[str, Any] | None:
        """过期发送事实仍只认领query。"""
        return await self.store.claim_compliance(allow_dispatch=allow_dispatch)

    async def renew_settlement(self, job: dict[str, Any]) -> None:
        """由原执行器心跳续租独立F3工作。"""
        await self.store.renew_compliance(job)

    async def release_settlement(self, job: dict[str, Any]) -> None:
        """只释放本租约，不改原发送事实。"""
        await self.store.release_compliance(job)

    async def prepare_settlement_dispatch(self, job: dict[str, Any]) -> dict[str, Any]:
        """材料先在事务外固定，随后事务内再次复核并记dispatching。"""
        if job["action"] == "archive":
            from xuanpu_finance.errors import FinanceError

            if self.archive is None:
                raise FinanceError("ARCHIVE_UNCONFIGURED", 503, "归档管线未配置")
            await self.archive.preflight(job["request_template"])
        return await self.store.prepare_compliance_dispatch(job)

    async def record_settlement_result(self, job: dict[str, Any], result: dict[str, Any],
                                       raw: bytes) -> None:
        """实际回执由领域二次校验后提交。"""
        await self.store.record_compliance_result(job, result, raw)

    async def defer_settlement(self, job: dict[str, Any], code: str, *, uncertain: bool) -> None:
        """模糊发送不能改为可重新提交。"""
        await self.store.defer_compliance(job, code, uncertain=uncertain)

    async def reject_settlement_before_dispatch(self, job: dict[str, Any], code: str) -> None:
        """只有明确未发送错误才能终结为拒绝。"""
        await self.store.reject_compliance_before_dispatch(job, code)

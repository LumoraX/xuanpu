"""按通讯录对账，复用身份维护周期与停用流水线。

不使用额外调度框架：已有维护循环足以触发，存储租约负责跨实例互斥。每个出站批次、每名员工及
完成步骤前原子续期；每步最多 30 秒，租约 120 秒，失租停止。无整租户时限，大租户可持续续期。
令牌仅存内存，按租户、应用与密钥摘要隔离；每次使用核对当前密钥，提前五分钟更新。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, InvalidRequestError, RuntimeUnavailableError
from xuanpu_server.identity.events import reason_from_status
from xuanpu_server.identity.feishu import MAX_BATCH_USERS, AccessToken, FeishuError
from xuanpu_server.identity.limits import CallLimitBusyError
from xuanpu_server.tenancy import system_principal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.feishu import FeishuClient, FeishuContactUser
    from xuanpu_server.identity.models import ExternalIdentity, TenantIdentityConfig
    from xuanpu_server.identity.service import IdentityService
    from xuanpu_server.metrics import PlatformMetrics
    from xuanpu_server.secrets import SecretVault

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120.0
STEP_SECONDS = 30.0


class IdentityReconcile:
    """一次认领只属于一个运行，成功才推进周期；失败保留已执行的停用。"""

    def __init__(
        self,
        service: IdentityService,
        *,
        control_plane: ControlPlaneStore,
        vault: SecretVault,
        audit: AuditLog,
        feishu: FeishuClient,
        metrics: PlatformMetrics | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """复用服务的存储、出站限流器和停用入口。"""
        self.service = service
        self.store = service.store
        self.feishu = feishu
        self._control = control_plane
        self._vault = vault
        self._audit = audit
        self._metrics = metrics
        self._now = now
        self._tokens: dict[str, tuple[str, str, AccessToken, float]] = {}

    async def run_due(self) -> None:
        """逐租户对账；一个租户失败不影响其他租户及下一轮。"""
        async with asyncio.timeout(STEP_SECONDS):
            configs = await self.store.list_identity_configs("feishu")
        for config in configs:
            try:
                async with asyncio.timeout(STEP_SECONDS):
                    tenant = await self._control.get_tenant(config.tenant_id)
                if tenant is None or tenant.status != "active" or not config.feishu_tenant_key:
                    continue
                await self.run(config.tenant_id)
            except ConflictError:
                continue  # 周期未到或另一个实例已经在跑
            except Exception:
                logger.exception("租户 %s 对账失败，下一维护周期重试", config.tenant_id)

    async def run(self, tenant_id: str, *, manual: bool = False) -> dict[str, int]:
        """执行一次对账；手工忽略周期，但仍与周期任务互斥。"""
        owner = secrets.token_hex(16)
        try:
            async with asyncio.timeout(STEP_SECONDS):
                config = await self.store.get_identity_config(tenant_id, "feishu")
                tenant = await self._control.get_tenant(tenant_id)
                if (
                    config is None
                    or not config.feishu_tenant_key
                    or tenant is None
                    or tenant.status != "active"
                ):
                    raise InvalidRequestError("租户尚未启用并绑定飞书企业")
                claimed = await self.store.claim_reconcile(
                    tenant_id,
                    owner=owner,
                    now=self._now(),
                    interval=0 if manual else self.service.settings.reconcile_interval_seconds,
                    lease_seconds=LEASE_SECONDS,
                )
        except TimeoutError as exc:
            raise RuntimeUnavailableError("对账存储暂不可用，请稍后重试") from exc
        if not claimed:
            raise ConflictError("对账正在执行或尚未到对账周期")
        counts = dict(checked=0, disabled=0, unverified=0)
        try:
            await self._scan(config, owner, counts)
            async with self._step(tenant_id, owner):
                if not await self.store.finish_reconcile(tenant_id, owner=owner, now=self._now()):
                    raise RuntimeUnavailableError("对账租约已失效")
        except BaseException as exc:
            self._run_metric("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
            await self._failure(tenant_id, counts, exc)
            if isinstance(exc, (FeishuError, CallLimitBusyError, TimeoutError)):
                raise RuntimeUnavailableError("飞书对账暂不可用，请稍后重试") from exc
            raise
        finally:
            try:
                async with asyncio.timeout(STEP_SECONDS):
                    await self.store.release_reconcile(tenant_id, owner=owner)
            except Exception:
                logger.exception("租户 %s 对账租约释放失败，等待租约到期", tenant_id)

        # 完成已持久化，之后审计不可用不能再反写失败终态。
        self._run_metric("succeeded")
        async with asyncio.timeout(STEP_SECONDS):
            await self._audit.control(
                system_principal(tenant_id, "reconcile"), tenant_id, "identity_reconciled",
                {**counts, "result": "succeeded"},
            )
        return counts

    async def _failure(self, tenant_id: str, counts: dict[str, int], exc: BaseException) -> None:
        """失败审计有界，写不进时保留原异常并留日志，不阻止释放。"""
        try:
            async with asyncio.timeout(STEP_SECONDS):
                await self._audit.control(
                    system_principal(tenant_id, "reconcile"),
                    tenant_id,
                    "identity_reconciled",
                    {**counts, "result": "failed", "error": type(exc).__name__},
                )
        except Exception:
            logger.exception("租户 %s 对账失败审计写入失败", tenant_id)

    @asynccontextmanager
    async def _step(self, tenant_id: str, owner: str) -> AsyncIterator[None]:
        """先原子续租再执行有界步骤；旧持有者不能复活过期租约。"""
        async with asyncio.timeout(STEP_SECONDS):
            if not await self.store.renew_reconcile(
                tenant_id,
                owner=owner,
                now=self._now(),
                lease_seconds=LEASE_SECONDS,
            ):
                raise RuntimeUnavailableError("对账租约已失效")
            yield

    async def _scan(self, config: TenantIdentityConfig, owner: str, counts: dict[str, int]) -> None:
        """按 subject 游标分页，停用导致 active 集合缩小时也不会跳过员工。"""
        after: str | None = None
        while True:
            async with self._step(config.tenant_id, owner):
                identities = await self.store.list_external_identities(
                    config.tenant_id,
                    "feishu",
                    user_status="active",
                    after_subject=after,
                    limit=MAX_BATCH_USERS,
                )
                if not identities:
                    return
                async with self.service.login.feishu_limit.slot(config.tenant_id):
                    token = await self._token(config)
                    contacts = await self.feishu.batch_users(
                        token, [item.subject for item in identities]
                    )
            found = {person.union_id: person for person in contacts}
            for identity in identities:
                async with self._step(config.tenant_id, owner):
                    await self._person(identity, found.get(identity.subject), counts)
            after = identities[-1].subject

    async def _token(self, config: TenantIdentityConfig) -> AccessToken:
        """缓存按应用与最新密钥摘要核对，五分钟内将过期则重新获取。"""
        secret = await self._vault.get(config.tenant_id, config.app_secret_ref)
        digest = hashlib.sha256(secret.encode()).hexdigest()
        cached = self._tokens.get(config.tenant_id)
        if cached and cached[:2] == (config.app_id, digest) and cached[3] > self._now() + 300:
            return cached[2]
        issued = self._now()
        token = await self.feishu.tenant_access_token(app_id=config.app_id, app_secret=secret)
        self._tokens[config.tenant_id] = (config.app_id, digest, token, issued + token.expires_in)
        return token

    async def _person(
        self,
        identity: ExternalIdentity,
        contact: FeishuContactUser | None,
        counts: dict[str, int],
    ) -> None:
        """查不到仅标记，再次查到清标记；明确离职状态才停用。"""
        tenant_id, user_id = identity.tenant_id, identity.user_id
        actor = system_principal(tenant_id, "reconcile")
        counts["checked"] += 1
        self._user_metric("checked")
        if contact is None:
            if await self.store.mark_user_unverified(tenant_id, user_id, at=self._now()):
                await self._audit.control(actor, tenant_id, "identity_unverified", user_id=user_id)
            counts["unverified"] += 1
            self._user_metric("unverified")
            return
        await self.store.clear_user_unverified(tenant_id, user_id)
        reason = reason_from_status(
            is_resigned=contact.is_resigned,
            is_exited=contact.is_exited,
            is_frozen=contact.is_frozen,
        )
        if reason is not None:
            outcome = await self.service.disable_user(
                tenant_id, user_id, reason, actor, keep_an_admin=False
            )
            if outcome.changed:
                counts["disabled"] += 1
                self._user_metric("disabled")

    def _run_metric(self, result: str) -> None:
        """记录有限枚举的运行结果。"""
        if self._metrics is not None:
            self._metrics.reconcile_run(result)

    def _user_metric(self, outcome: str) -> None:
        """记录处理计数，不带租户标签。"""
        if self._metrics is not None:
            self._metrics.reconcile_user(outcome)

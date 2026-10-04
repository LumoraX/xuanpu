"""飞书事件回调的编排：``POST /v1/identity/feishu/events/{tenant_id}``（设计 7.3，另有调整）。

不走平台的认证，靠飞书的签名认来源。纯计算（验签、解密、解析）在 ``identity.events``；这里取配置与
密钥、核对来源、停用员工。处理顺序：

1. 请求体超过 1 MiB → ``400``（接入层最多读到上限多一个字节就停）；
2. 取租户的飞书配置与它引用的 Encrypt Key、Verification Token：租户标识不合法、没有配置、密钥不在
   保管库里 → ``404``（后一种另记告警：配置坏了）；
3. **没有签名头**：只认加密的 URL 验证——能用 Encrypt Key 解开、``type`` 是
   ``url_verification``、``token`` 等于 Verification Token，就原样回 ``{"challenge": …}``。别的
   一概 ``401``，而且不论哪一步没过（包括没料到的异常）都是**同一个响应**：没有签名的请求谁都能
   发，各步的错误要是分得清，就成了能逐字节猜明文的 CBC 填充预言机。没加密的 URL 验证同样 401
   （平台要求 Encrypt Key），日志里提示去飞书后台配置；
4. **有签名头**：按设计第 3 节验签（常数时间比较），不符或缺时间戳、随机数 → ``401``；然后解密，
   内容不合预期 → ``400``（签名对了说明是飞书发的，内容不对是配置或飞书的问题，不必再藏）；
5. URL 验证（带了签名也认）：同 3；
6. 2.0 版事件：``header.token`` 等于 Verification Token（常数时间比较）、``header.app_id`` 等于
   配置的 App ID、``header.tenant_key`` 等于绑定的飞书企业，不符 → ``401``；
7. 租户还没绑定飞书企业（还没有员工登录过）→ ``200`` 忽略并记日志：核对不了 ``tenant_key``，也不
   拿事件去绑定（绑定只能由真实的登录建立）；这时租户里也还没有有飞书身份的员工，没什么可停用的；
8. ``contact.user.deleted_v3``：按 ``event.object.union_id`` 找员工（找不到 → ``200`` 忽略并记
   日志），同步做停用流水线的第 1、2 步（标停用、删登录会话；``keep_an_admin=False``——离职的管理员
   也停用），第 3 步（关会话等）放后台，回 ``200``。原因：``is_resigned`` → ``feishu_resigned``，
   ``is_exited`` → ``feishu_exited``，``is_frozen`` → ``feishu_frozen``，都没有按
   ``feishu_resigned``（事件本身就是离职）。``actor`` 是 ``system:feishu-event``，``user_disabled``
   里另记 ``event_id``；
9. 别的事件类型、1.0 版事件 → ``200`` 忽略。

**与设计的不同**（记在实现计划的对齐表里）：设计 7.3 先验签再处理 URL 验证，但飞书发来的 URL 验证
不带签名头（见 ``identity.events``），照设计做 URL 验证永远过不了，所以改为第 3 步的做法。

**不防重放、不设时间窗**（设计 7.3）：停用可以重复执行，飞书重推（15 秒、5 分钟、1 小时、6 小时，
最多 4 次；不清楚重推时是否换时间戳）与重放都只会重复一次无害的操作。重放一条旧的离职事件能把一名
被重新启用的员工再停用一次——这要截获到 HTTPS 上的飞书推送；而员工在飞书里仍是离职的，对账也会
再停用他。

**签名不符、来源核对不过一律不写审计**（防止被刷），只记日志、计 ``xuanpu_feishu_events_total``。
停用本身照常记审计。存储报错、保管库里的密文解不开这类意外照常上抛（接入层回 500），计
``internal_error``。
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import (
    AuthenticationError,
    InvalidRequestError,
    NotFoundError,
    PlatformError,
    SecretMissingError,
)
from xuanpu_server.identity import events
from xuanpu_server.identity.disable import FEISHU_EVENT_ACTOR
from xuanpu_server.identity.events import EventFormatError, UnsupportedPayload, UrlVerification
from xuanpu_server.identity.login import FEISHU
from xuanpu_server.tenancy import system_principal, validate_tenant_id

if TYPE_CHECKING:
    from xuanpu_server.identity.disable import DisablePipeline
    from xuanpu_server.identity.events import EventEnvelope
    from xuanpu_server.identity.models import TenantIdentityConfig
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.metrics import FeishuEventResult, PlatformMetrics
    from xuanpu_server.secrets import SecretVault

logger = logging.getLogger(__name__)

SUCCESS: dict[str, Any] = {"msg": "success"}
"""处理完（或忽略）一个事件时的响应体；飞书只看状态码。"""


class CallbackRejectedError(Exception):
    """回调被拒（流程内部用：在最外层统一计数、记日志、转成平台异常）。

    Attributes:
        result: 指标的取值。
        error: 回给飞书的平台异常（文案统一，不含细节）。
        detail: 只写进服务端日志的说明，不含机密与请求内容。
        level: 日志级别。
    """

    def __init__(
        self, result: FeishuEventResult, error: PlatformError, detail: str, *, level: int
    ) -> None:
        """记下结果与说明。"""
        super().__init__(detail)
        self.result: FeishuEventResult = result
        self.error = error
        self.detail = detail
        self.level = level


def _unauthenticated(result: FeishuEventResult, detail: str) -> CallbackRejectedError:
    """签名或来源不对：统一的 401。"""
    error = AuthenticationError("凭据无效")
    return CallbackRejectedError(result, error, detail, level=logging.WARNING)


def _bad_request(detail: str) -> CallbackRejectedError:
    """签名对了、内容不合预期，或请求体过大：400。"""
    error = InvalidRequestError(f"事件请求不合预期：{detail}")
    return CallbackRejectedError("bad_request", error, detail, level=logging.WARNING)


def _unknown_tenant(detail: str, *, level: int = logging.INFO) -> CallbackRejectedError:
    """租户没有可用的飞书配置：404，同一句提示。"""
    error = NotFoundError("这个租户没有可用的飞书事件配置")
    return CallbackRejectedError("unknown_tenant", error, detail, level=level)


@dataclass(frozen=True)
class _Secrets:
    """验签、解密、核对来源要用的两个密钥的明文。"""

    encrypt_key: str
    verification_token: str

    def token_matches(self, token: str) -> bool:
        """Verification Token 对不对（常数时间比较）。"""
        return hmac.compare_digest(token.encode("utf-8"), self.verification_token.encode("utf-8"))


class FeishuEventCallback:
    """飞书事件回调。由 ``IdentityService`` 组合，接入层经 ``IdentityService.events`` 调用。"""

    def __init__(
        self,
        *,
        store: IdentityStore,
        vault: SecretVault,
        disabling: DisablePipeline,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        """
        Args:
            store: 身份存储（租户的飞书配置、按飞书身份找员工）。
            vault: 密钥保管库（Encrypt Key、Verification Token）。
            disabling: 停用流水线。
            metrics: 平台指标；None 不打点。
        """
        self._store = store
        self._vault = vault
        self._disabling = disabling
        self._metrics = metrics

    async def handle(
        self,
        tenant_id: str,
        *,
        body: bytes,
        signature: str | None,
        timestamp: str | None,
        nonce: str | None,
    ) -> dict[str, Any]:
        """处理一个回调请求，返回响应体（顺序见模块说明）。

        Raises:
            InvalidRequestError: 请求体过大，或签名对了而内容不合预期（400）。
            AuthenticationError: 签名或来源不对（401）。
            NotFoundError: 租户没有可用的飞书配置（404）。
            Exception: 别的异常（存储报错、保管库里的密文解不开等）原样上抛，由接入层按 500 处理；
                计 ``internal_error``。
        """
        try:
            result, reply = await self._handle(tenant_id, body, signature, timestamp, nonce)
        except CallbackRejectedError as rejected:
            self._count(rejected.result)
            # 路径里的租户标识可能是任意字符串，用 repr 记，免得往日志里注入换行
            logger.log(
                rejected.level, "飞书事件回调（租户 %r）被拒：%s", tenant_id, rejected.detail
            )
            raise rejected.error from None
        except Exception:
            self._count("internal_error")
            raise
        self._count(result)
        return reply

    async def _handle(
        self,
        tenant_id: str,
        body: bytes,
        signature: str | None,
        timestamp: str | None,
        nonce: str | None,
    ) -> tuple[FeishuEventResult, dict[str, Any]]:
        """各步，被拒时抛 ``CallbackRejectedError``。"""
        if len(body) > events.MAX_EVENT_BODY_BYTES:
            raise _bad_request(f"请求体超过 {events.MAX_EVENT_BODY_BYTES} 字节")
        config = await self._config(tenant_id)
        secrets = await self._secrets(config)
        if signature is None:
            return "url_verification", self._unsigned(secrets, body)
        if timestamp is None or nonce is None:
            raise _unauthenticated("bad_signature", "带了签名，却缺时间戳或随机数")
        if not events.signature_matches(
            signature, timestamp=timestamp, nonce=nonce, encrypt_key=secrets.encrypt_key, body=body
        ):
            raise _unauthenticated("bad_signature", "签名不符")
        payload = self._open(secrets, body)
        if isinstance(payload, UrlVerification):
            return "url_verification", self._challenge(secrets, payload)
        if isinstance(payload, UnsupportedPayload):
            logger.info("飞书事件回调（租户 %s）：%s，忽略", tenant_id, payload.description)
            return "ignored", SUCCESS
        return await self._event(config, secrets, payload), SUCCESS

    async def _config(self, tenant_id: str) -> TenantIdentityConfig:
        """租户的飞书配置。"""
        try:
            validate_tenant_id(tenant_id)
        except ValueError as exc:
            raise _unknown_tenant("租户标识不合法") from exc
        config = await self._store.get_identity_config(tenant_id, FEISHU)
        if config is None:
            raise _unknown_tenant("租户不存在或没有配置飞书")
        return config

    async def _secrets(self, config: TenantIdentityConfig) -> _Secrets:
        """取 Encrypt Key 与 Verification Token 的明文。

        缺任一引用时事件明确关闭；不在保管库里的，按没有可用配置处理（另记告警）。
        """
        encrypt_ref, verification_ref = config.encrypt_key_ref, config.verification_token_ref
        if not encrypt_ref or not verification_ref:
            raise _unknown_tenant("飞书事件已关闭：Encrypt Key 与 Verification Token 引用尚未配齐")
        try:
            return _Secrets(
                encrypt_key=await self._vault.get(config.tenant_id, encrypt_ref),
                verification_token=await self._vault.get(
                    config.tenant_id, verification_ref
                ),
            )
        except SecretMissingError as exc:
            refs = f"{config.encrypt_key_ref}、{config.verification_token_ref}"
            raise _unknown_tenant(
                f"飞书配置引用的密钥（{refs}）不全在保管库里，验不了签", level=logging.WARNING
            ) from exc

    def _unsigned(self, secrets: _Secrets, body: bytes) -> dict[str, Any]:
        """没有签名头：只认加密的 URL 验证；不论哪一步没过都是同一个 401（模块说明第 3 步）。

        不只接 ``EventFormatError``：这条路谁都能走，任何异常（哪怕是没料到的）都转成同一个 401，
        细节（``EventFormatError`` 的说明，或别的异常的类型名）只进日志。
        """
        try:
            return self._answer_unsigned(secrets, body)
        except CallbackRejectedError:
            raise
        except Exception as exc:
            detail = str(exc) if isinstance(exc, EventFormatError) else type(exc).__name__
            raise _unauthenticated("bad_signature", f"没有签名，也解不开：{detail}") from exc

    @staticmethod
    def _answer_unsigned(secrets: _Secrets, body: bytes) -> dict[str, Any]:
        """``_unsigned`` 的各步。外层请求体只解析一次，没加密的 URL 验证在同一个对象上认出来。"""
        outer = events.outer_object(body)
        if events.is_plain_url_verification(outer):
            raise _unauthenticated(
                "bad_signature",
                "收到没有加密的 URL 验证：飞书后台没有配置 Encrypt Key（平台要求配置，"
                "否则之后的事件都验不了签）",
            )
        payload = FeishuEventCallback._decrypt(secrets, outer)
        if not isinstance(payload, UrlVerification):
            raise _unauthenticated("bad_signature", "没有签名，而且不是 URL 验证")
        if not secrets.token_matches(payload.token):
            raise _unauthenticated("bad_signature", "没有签名的 URL 验证，Verification Token 不符")
        return {"challenge": payload.challenge}

    def _open(self, secrets: _Secrets, body: bytes) -> events.Payload:
        """解开已验过签的请求体；内容不合预期是 400。"""
        try:
            return self._decrypt(secrets, events.outer_object(body))
        except EventFormatError as exc:
            raise _bad_request(str(exc)) from exc

    @staticmethod
    def _decrypt(secrets: _Secrets, outer: dict[str, Any]) -> events.Payload:
        """取外层对象里的 ``encrypt``、解密、认出内容。"""
        plaintext = events.decrypt(secrets.encrypt_key, events.encrypted_field(outer))
        return events.parse_payload(plaintext)

    @staticmethod
    def _challenge(secrets: _Secrets, payload: UrlVerification) -> dict[str, Any]:
        """带了签名的 URL 验证：Verification Token 对了就回 challenge。"""
        if not secrets.token_matches(payload.token):
            raise _unauthenticated("rejected", "URL 验证的 Verification Token 不符")
        return {"challenge": payload.challenge}

    async def _event(
        self, config: TenantIdentityConfig, secrets: _Secrets, envelope: EventEnvelope
    ) -> FeishuEventResult:
        """一个验过签的 2.0 版事件：核对来源，离职事件停用员工，别的忽略。"""
        tenant_id = config.tenant_id
        if not secrets.token_matches(envelope.token):
            raise _unauthenticated("rejected", "事件的 Verification Token 不符")
        if envelope.app_id != config.app_id:
            raise _unauthenticated("rejected", f"事件的 App ID {envelope.app_id!r} 与配置的不一致")
        if config.feishu_tenant_key is None:
            logger.info(
                "飞书事件回调（租户 %s）：还没绑定飞书企业（还没有员工登录过），"
                "核对不了 tenant_key，%s 不处理",
                tenant_id,
                envelope.event_type,
            )
            return "ignored"
        if envelope.tenant_key != config.feishu_tenant_key:
            raise _unauthenticated("rejected", "事件的 tenant_key 与租户绑定的飞书企业不一致")
        if envelope.event_type != events.USER_DELETED:
            return "ignored"
        return await self._user_deleted(tenant_id, envelope)

    async def _user_deleted(self, tenant_id: str, envelope: EventEnvelope) -> FeishuEventResult:
        """员工离职：同步做停用的第 1、2 步，第 3 步放后台。"""
        try:
            deleted = events.user_deleted(envelope)
        except EventFormatError as exc:
            raise _bad_request(str(exc)) from exc
        user = await self._store.get_user_by_identity(tenant_id, FEISHU, deleted.union_id)
        if user is None:
            logger.info("飞书离职事件（租户 %s）：平台里没有这名员工，忽略", tenant_id)
            return "ignored"
        actor = system_principal(tenant_id, FEISHU_EVENT_ACTOR)
        detail = {"event_id": envelope.event_id} if envelope.event_id else None
        await self._disabling.mark_disabled(
            tenant_id, user.user_id, deleted.reason, actor, keep_an_admin=False, detail=detail
        )
        self._disabling.notify_later(tenant_id, user.user_id, actor)
        return "accepted"

    def _count(self, result: FeishuEventResult) -> None:
        """记一次处理结果的指标。"""
        if self._metrics is not None:
            self._metrics.feishu_event(result)


__all__ = ["SUCCESS", "CallbackRejectedError", "FeishuEventCallback"]

"""可替换的飞书通知出口：调度与站内通知不依赖它。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

import httpx

from xuanpu_server.identity.feishu import FeishuClient, FeishuError
from xuanpu_server.reminders.models import DeliveryError
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.models import TenantIdentityConfig
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.reminders.models import NotificationRecord
    from xuanpu_server.secrets import SecretVault


class FeishuNotificationSender:
    """只发送已落库且用户选中飞书渠道的通知；日志不含密钥或正文。"""

    def __init__(
        self,
        *,
        control: ControlPlaneStore,
        identity: IdentityStore,
        vault: SecretVault,
        open_base: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """依赖由平台注入，复用企业管理员已有的应用配置。"""
        self._control, self._identity, self._vault = control, identity, vault
        self._base = open_base.rstrip("/")
        self._tokens = FeishuClient(
            open_base, "https://accounts.feishu.cn", transport=transport, timeout_seconds=5
        )
        self._http = httpx.AsyncClient(
            transport=transport, timeout=5, trust_env=False, follow_redirects=False
        )

    async def _target(self, who: Principal) -> tuple[TenantIdentityConfig, str] | None:
        """每次发送重查员工、租户、企业配置与该员工的外部身份映射。"""
        tenant = who.require_tenant()
        if not who.user_id:
            return None
        organization = await self._control.get_tenant(tenant)
        user = await self._identity.get_user(tenant, who.user_id)
        if organization and organization.status != "active" and user and user.status == "active":
            raise DeliveryError("tenant_paused", retryable=True)
        if (
            not organization
            or organization.status != "active"
            or not user
            or user.status != "active"
        ):
            return None
        config = await self._identity.get_identity_config(tenant, "feishu")
        if not config or not config.feishu_tenant_key:
            return None
        identities = await self._identity.list_external_identities(
            tenant, "feishu", user_id=who.user_id, limit=1
        )
        open_id = identities[0].attributes.get("open_id") if identities else None
        if not open_id or config.app_secret_ref not in await self._vault.names(tenant):
            return None
        return config, open_id

    async def available(self, who: Principal) -> list[str]:
        """仅表示已配置目的地；发送权限仍由飞书在真实请求时判断。"""
        try:
            return ["feishu"] if await self._target(who) else []
        except DeliveryError as exc:
            if exc.code == "tenant_paused":
                return []
            raise

    async def send(self, notification: NotificationRecord, channel: str) -> None:
        """有界发送，稳定UUID降低重试重复；任何失败均返回安全分类。"""
        if channel != "feishu":
            raise DeliveryError("unsupported_channel", retryable=False)
        who = Principal("tenant_member", notification.tenant_id, None, notification.user_id)
        try:
            async with asyncio.timeout(9):
                target = await self._target(who)
                if target is None:
                    raise DeliveryError("channel_unavailable", retryable=False)
                config, open_id = target
                secret = await self._vault.get(notification.tenant_id, config.app_secret_ref)
                token = await self._tokens.tenant_access_token(
                    app_id=config.app_id, app_secret=secret
                )
                current = await self._target(who)
                if current is None:
                    raise DeliveryError('channel_unavailable', retryable=False)
                if current != target or await self._vault.get(notification.tenant_id, config.app_secret_ref) != secret:
                    raise DeliveryError('channel_changed', retryable=True)
                due = datetime.fromtimestamp(notification.due_at, UTC).isoformat()
                identity = f"xuanpu:{notification.tenant_id}:{notification.reminder_id}:{notification.due_at}"
                body = {
                    "receive_id": open_id,
                    "msg_type": "text",
                    "uuid": str(uuid5(NAMESPACE_URL, identity)),
                    "content": json.dumps(
                        {
                            "text": f"玄圃提醒\n{notification.title}\n原定时间：{due}\n请在玄圃通知中心查看。"
                        },
                        ensure_ascii=False,
                    ),
                }
                await self._post(body, token.value)
        except FeishuError as exc:
            raise DeliveryError(
                "feishu_unavailable" if exc.kind == "unavailable" else "feishu_denied",
                retryable=exc.kind == "unavailable",
            ) from None
        except (httpx.HTTPError, TimeoutError):
            raise DeliveryError("feishu_unavailable", retryable=True) from None

    async def _post(self, body: dict[str, str], token: str) -> None:
        """限定响应大小，忽略供应商原始错误文本。"""
        async with self._http.stream(
            "POST",
            self._base + "/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            json=body,
            headers={"Authorization": "Bearer " + token},
        ) as response:
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise DeliveryError("feishu_invalid_response", retryable=True)
            if response.status_code == 429 or response.status_code >= 500:
                raise DeliveryError("feishu_unavailable", retryable=True)
            try:
                result: Any = json.loads(raw)
            except ValueError:
                raise DeliveryError("feishu_invalid_response", retryable=True) from None
            if (
                response.status_code != 200
                or not isinstance(result, dict)
                or type(result.get("code")) is not int
                or result.get("code") != 0
            ):
                raise DeliveryError("feishu_denied", retryable=False)
            data = result.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("message_id"), str) or not data["message_id"]:
                raise DeliveryError("feishu_invalid_response", retryable=True)

    async def close(self) -> None:
        """关闭两个连接池；后台投递必须先停止。"""
        await self._tokens.aclose()
        await self._http.aclose()

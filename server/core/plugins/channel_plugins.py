"""飞书 / 企业微信主动触达与卡片一键拍板 L2 通道插件（ADR 0027）。

- 把管家晨报、会前 30 分钟速览、长期静默追踪动态与高风险审批/裁决卡片格式化为
  飞书 Interactive Card 与企业微信 Template Card；
- 所有卡片操作按钮附带 HMAC-SHA256 签名令牌，回调验签严格阻断伪造确认；
- 绑定关系与投递回执全部持久化至共享 ``PluginCollectionStore``（支持 PG 多节点与本地重启）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from server.core.errors import InvalidRequestError, NotFoundError, PermissionDeniedError
from server.core.ids import new_id
from server.core.plugins.models import BackendPluginManifest, PluginHealth

if TYPE_CHECKING:
    from server.core.plugins.host import PluginContext
    from server.core.tenancy import Principal

ChannelKind = Literal["feishu", "wecom"]
CardKind = Literal["daily_brief", "pre_meeting_brief", "topic_watch", "approval_card"]


class ChannelBinding(BaseModel):
    """员工的主动触达 IM 通道绑定配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    channel: ChannelKind
    target_id: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    card_types: tuple[CardKind, ...] = ("daily_brief", "pre_meeting_brief", "topic_watch", "approval_card")


class ChannelTransport(Protocol):
    """底层 IM 卡片发送器接口（生产对接飞书/企微开放平台，测试可注入确定性记录器）。"""

    async def send_card(
        self, channel: ChannelKind, target_id: str, card_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """发送一张格式化交互卡片并返回通道回执。"""
        ...


class DryRunChannelTransport:
    """默认安全空转发送器（未配置真实 IM 凭据时仅记录本地回执，不发外网）。"""

    async def send_card(
        self, channel: ChannelKind, target_id: str, card_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """返回模拟已受理回执。"""
        return {
            "accepted": True,
            "mode": "dry_run",
            "channel": channel,
            "target_id": target_id,
            "card_type": card_payload.get("schema") or card_payload.get("msgtype") or "card",
        }


def sign_card_callback(secret: str, *, card_id: str, action: str, user_id: str) -> str:
    """为卡片拍板回调生成 HMAC-SHA256 防伪造签名。"""
    msg = f"{card_id}:{action}:{user_id}".encode()
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def build_feishu_card(
    *, card_id: str, card_kind: CardKind, title: str, body: str, actions: list[dict[str, str]],
) -> dict[str, Any]:
    """构造飞书 Interactive Card JSON 负载。"""
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": body[:3000]},
    ]
    if actions:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": a["label"]},
                    "type": "primary" if a["action"] == "approve" else "default",
                    "value": {
                        "card_id": card_id,
                        "action": a["action"],
                        "signature": a["signature"],
                    },
                }
                for a in actions
            ],
        })
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": title[:100]}, "template": "blue"},
        "body": {"elements": elements},
        "meta": {"card_id": card_id, "card_kind": card_kind},
    }


def build_wecom_card(
    *, card_id: str, card_kind: CardKind, title: str, body: str, actions: list[dict[str, str]],
) -> dict[str, Any]:
    """构造企业微信 Template Card 交互卡片负载。"""
    return {
        "msgtype": "template_card",
        "template_card": {
            "card_type": "button_interaction" if actions else "text_notice",
            "main_title": {"title": title[:64]},
            "sub_title_text": body[:500],
            "task_id": card_id,
            "button_list": [
                {
                    "text": a["label"],
                    "style": 1 if a["action"] == "approve" else 2,
                    "key": f"{card_id}:{a['action']}:{a['signature']}",
                }
                for a in actions
            ],
        },
        "meta": {"card_id": card_id, "card_kind": card_kind},
    }


class ImOutreachChannelPlugin:
    """L2 主动触达通道插件：打通飞书与企业微信卡片投递及签名回调拍板。"""

    manifest = BackendPluginManifest(
        plugin_id="im-outreach",
        version="1.0.0",
        display_name="飞书/企微主动触达通道",
        description="向飞书与企业微信推送晨报、会前速览、追踪动态与带 HMAC 签名防伪的拍板审批卡。",
        trust_level="in_process_verified",
        permissions=("collection.read", "collection.write", "channel.send"),
        collections=("bindings", "deliveries"),
    )

    def __init__(
        self,
        *,
        transport: ChannelTransport | None = None,
        callback_secret: str | None = None,
    ) -> None:
        """注入底层 IM 发送器与回调签名密钥。"""
        self._transport: ChannelTransport = transport or DryRunChannelTransport()
        self._secret = callback_secret or secrets.token_hex(24)
        self._ctx: PluginContext | None = None

    async def mount(self, ctx: PluginContext) -> None:
        """注册绑定管理、卡片投递与签名拍板回调动态路由。"""
        self._ctx = ctx
        ctx.add_route("GET", "binding", self._get_binding)
        ctx.add_route("PUT", "binding", self._put_binding)
        ctx.add_route("POST", "dispatch", self._dispatch_card)
        ctx.add_route("POST", "card-callback", self._card_callback)

    async def health(self) -> PluginHealth:
        """探活检查。"""
        ok = self._ctx is not None
        return PluginHealth(ok=ok, detail="mounted" if ok else "unmounted", checked_at=datetime.now(UTC))

    async def unmount(self, *, drain_timeout: float = 15.0) -> None:
        """干净卸载。"""
        _ = drain_timeout
        self._ctx = None

    async def _get_binding(self, who: Principal, _sub: str, _payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        try:
            rec = await self._ctx.collections.get(who, self.manifest.plugin_id, "bindings", "primary")
            return {"bound": True, "binding": rec.data, "version": rec.version}
        except NotFoundError:
            return {"bound": False, "binding": None}

    async def _put_binding(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        binding = ChannelBinding.model_validate(payload)
        saved = await self._ctx.collections.put(
            who, self.manifest.plugin_id, "bindings", binding.model_dump(mode="json"), record_id="primary",
        )
        return {"bound": True, "binding": saved.data, "version": saved.version}

    async def _dispatch_card(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        rec = await self._ctx.collections.get(who, self.manifest.plugin_id, "bindings", "primary")
        binding = ChannelBinding.model_validate(rec.data)
        if not binding.enabled:
            return {"delivered": False, "reason": "channel_disabled"}
        card_kind: CardKind = payload.get("card_kind", "daily_brief")
        if card_kind not in binding.card_types:
            return {"delivered": False, "reason": "card_type_muted"}
        title = str(payload.get("title") or "").strip()
        body = str(payload.get("body") or "").strip()
        if not title or not body:
            raise InvalidRequestError("卡片投递必须提供非空 title 与 body")

        card_id = new_id("imc")
        user_id = who.user_id or who.actor
        actions: list[dict[str, str]] = []
        if card_kind == "approval_card":
            for act, lbl in (("approve", "批准执行"), ("reject", "驳回")):
                actions.append({
                    "action": act,
                    "label": lbl,
                    "signature": sign_card_callback(self._secret, card_id=card_id, action=act, user_id=user_id),
                })
        else:
            actions.append({
                "action": "ignore",
                "label": "暂不关注",
                "signature": sign_card_callback(self._secret, card_id=card_id, action="ignore", user_id=user_id),
            })

        card_payload = (
            build_feishu_card(card_id=card_id, card_kind=card_kind, title=title, body=body, actions=actions)
            if binding.channel == "feishu"
            else build_wecom_card(card_id=card_id, card_kind=card_kind, title=title, body=body, actions=actions)
        )
        receipt = await self._transport.send_card(binding.channel, binding.target_id, card_payload)
        await self._ctx.collections.put(
            who,
            self.manifest.plugin_id,
            "deliveries",
            {
                "card_id": card_id,
                "channel": binding.channel,
                "card_kind": card_kind,
                "title": title,
                "status": "sent",
                "receipt": receipt,
            },
            record_id=card_id,
        )
        return {
            "delivered": True,
            "card_id": card_id,
            "channel": binding.channel,
            "card_payload": card_payload,
            "actions": actions,
            "receipt": receipt,
        }

    async def _card_callback(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        card_id = str(payload.get("card_id") or "").strip()
        action = str(payload.get("action") or "").strip()
        sig = str(payload.get("signature") or "").strip()
        if not card_id or action not in ("approve", "reject", "ignore") or not sig:
            raise InvalidRequestError("缺少合法的卡片回调参数")
        user_id = who.user_id or who.actor
        expected = sign_card_callback(self._secret, card_id=card_id, action=action, user_id=user_id)
        if not hmac.compare_digest(sig, expected):
            raise PermissionDeniedError("卡片回调签名校验失败，已拒绝伪造确认")

        rec = await self._ctx.collections.get(who, self.manifest.plugin_id, "deliveries", card_id)
        data = dict(rec.data)
        if data.get("status") != "sent":
            return {"status": data.get("status"), "idempotent": True, "card_id": card_id}
        data["status"] = f"acted:{action}"
        await self._ctx.collections.put(
            who, self.manifest.plugin_id, "deliveries", data, record_id=card_id, expected_version=rec.version,
        )
        # 员工点「暂不关注」时自动反馈到持久化 HabitCalibration（连续忽略 >= 3 次自动静音）
        if action == "ignore":
            await self._ctx.platform.butler.briefings.record_habit_ignored(
                who, str(data.get("card_kind") or "daily_brief"),
            )
        return {"status": data["status"], "idempotent": False, "card_id": card_id, "action": action}

"""飞书/企微主动触达卡片 L2 通道插件（含 HMAC 验签防伪与习惯反馈）及 Forge 控制面草稿镜像测试。"""

from __future__ import annotations

from typing import Any

import httpx

from server.api.app import create_app
from server.api.auth import caller
from server.core.plugins.channel_plugins import ImOutreachChannelPlugin
from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord


class _RecordingChannelTransport:
    """测试用 IM 卡片发送记录器。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def send_card(
        self, channel: str, target_id: str, card_payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.sent.append((channel, target_id, card_payload))
        return {"accepted": True, "msg_id": f"im_{len(self.sent)}"}


async def test_im_outreach_feishu_wecom_cards_hmac_callback_and_forge_mirror(
    world: Any,
) -> None:
    """验证飞书/企微卡片投递、HMAC 签名防伪拍板、忽略反馈进持久化 Habit 与 Forge 控制面镜像。"""
    platform = world.platform
    user_id = "usr_im_alice"
    await platform.identity.store.create_user(
        UserRecord(
            user_id=user_id, tenant_id="acme", role="tenant_member", status="active",
            display_name="Alice", created_at=1000.0,
        )
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id=user_id, key_id=None)

    transport = _RecordingChannelTransport()
    await platform.plugins.hot_swap(
        ImOutreachChannelPlugin(transport=transport, callback_secret="test-card-secret")
    )

    app = create_app(platform)
    app.dependency_overrides[caller] = lambda: who
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        # 1. 绑定飞书通道并投递审批卡片
        bind_fs = await http.put(
            "/v1/plugins/ext/im-outreach/binding",
            json={"channel": "feishu", "target_id": "ou_alice_123", "enabled": True},
        )
        assert bind_fs.status_code == 200 and bind_fs.json()["bound"] is True

        disp = await http.post(
            "/v1/plugins/ext/im-outreach/dispatch",
            json={
                "card_kind": "approval_card",
                "title": "请拍板：对外发送季度报价单",
                "body": "金额 120 万元，收件人：华辰科技采购部。",
            },
        )
        assert disp.status_code == 200, disp.text
        d_body = disp.json()
        assert d_body["delivered"] is True and d_body["channel"] == "feishu"
        assert d_body["card_payload"]["schema"] == "2.0"
        assert len(transport.sent) == 1
        card_id = d_body["card_id"]
        approve_sig = next(a["signature"] for a in d_body["actions"] if a["action"] == "approve")

        # 2. 伪造签名回调必须返回 403
        forged = await http.post(
            "/v1/plugins/ext/im-outreach/card-callback",
            json={"card_id": card_id, "action": "approve", "signature": "0" * 64},
        )
        assert forged.status_code == 403

        # 3. 合法签名回调成功且幂等
        ok_cb = await http.post(
            "/v1/plugins/ext/im-outreach/card-callback",
            json={"card_id": card_id, "action": "approve", "signature": approve_sig},
        )
        assert ok_cb.status_code == 200 and ok_cb.json()["status"] == "acted:approve"
        replay_cb = await http.post(
            "/v1/plugins/ext/im-outreach/card-callback",
            json={"card_id": card_id, "action": "approve", "signature": approve_sig},
        )
        assert replay_cb.status_code == 200 and replay_cb.json()["idempotent"] is True

        # 4. 切换企业微信通道投递晨报并点「暂不关注」-> 自动计入持久化 HabitCalibration
        bind_wc = await http.put(
            "/v1/plugins/ext/im-outreach/binding",
            json={"channel": "wecom", "target_id": "wx_alice", "enabled": True},
        )
        assert bind_wc.status_code == 200
        disp_wc = await http.post(
            "/v1/plugins/ext/im-outreach/dispatch",
            json={"card_kind": "daily_brief", "title": "今日要点", "body": "上午 9 点评审会。"},
        )
        assert disp_wc.status_code == 200
        wc_body = disp_wc.json()
        assert wc_body["card_payload"]["msgtype"] == "template_card"
        ign_sig = wc_body["actions"][0]["signature"]
        ign_cb = await http.post(
            "/v1/plugins/ext/im-outreach/card-callback",
            json={"card_id": wc_body["card_id"], "action": "ignore", "signature": ign_sig},
        )
        assert ign_cb.status_code == 200
        habit = await platform.butler.briefings.get_habit(who)
        assert habit.topic_ignored_streaks.get("daily_brief") == 1

    # 5. 验证 ArtisanForge 草稿同步镜像至统一控制面集合（跨节点可查）
    created = await platform.forge.create_draft(
        world.admin, package_name="mirror-bot", description="多节点草稿镜像验证", entry="main",
    )
    mirrored = await platform.forge.list_persisted_drafts(world.admin)
    assert any(d.draft_id == created.draft_id and d.package_name == "mirror-bot" for d in mirrored)

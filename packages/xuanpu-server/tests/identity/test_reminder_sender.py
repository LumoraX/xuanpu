"""飞书只是通知出口，不拥有提醒时钟；测试不向真实员工发送。"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from xuanpu_server.identity.models import ExternalIdentity
from xuanpu_server.reminders.models import DeliveryError, NotificationRecord
from xuanpu_server.tenancy import Principal


async def test_feishu_sender_uses_mapping_and_stable_key(identity_world: Any) -> None:
    """只用本人映射发送，重试保持uuid；配置不全时明确不可用。"""
    from xuanpu_server.reminders.feishu import FeishuNotificationSender
    async with identity_world() as ident:
        p = ident.world.platform
        user = await ident.user()
        who = Principal('tenant_member', 'acme', None, user.user_id)
        sent = []
        deactivate = False
        pause = False
        async def handler(request: httpx.Request) -> httpx.Response:
            """只接模拟的令牌与消息接口。"""
            if request.url.path.endswith('tenant_access_token/internal'):
                if pause:
                    tenant = await p.store.get_tenant("acme")
                    assert tenant is not None
                    await p.store.update_tenant(replace(tenant, status="disabled"))
                if deactivate:
                    await p.identity.disable_user("acme", user.user_id, "admin", ident.world.admin, keep_an_admin=False)
                return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'test-token', 'expire': 7200})
            sent.append(json.loads(request.content))
            assert request.url.params['receive_id_type'] == 'open_id'
            return httpx.Response(200, json={'code': 0, 'data': {'message_id': 'test-msg'}})
        sender = FeishuNotificationSender(control=p.store, identity=p.identity.store, vault=p.vault,
            open_base='https://feishu.test', transport=httpx.MockTransport(handler))
        try:
            assert await sender.available(who) == []
            await ident.configure_feishu()
            await ident.store.bind_tenant_key('acme', 'feishu', 'tk_acme')
            await ident.store.create_external_identity(ExternalIdentity('acme','feishu','union1',user.user_id,{'open_id':'own-open-id'}))
            assert await sender.available(who) == ['feishu']
            notification = NotificationRecord(1,'acme',user.user_id,'r1','准备复盘',123,124)
            await sender.send(notification,'feishu')
            await sender.send(notification,'feishu')
            assert sent[0]['uuid'] == sent[1]['uuid']
            assert sent[0]['receive_id'] == 'own-open-id'
            assert json.loads(sent[0]['content'])['text'].startswith('玄圃提醒')
            tenant = await p.store.get_tenant("acme")
            assert tenant is not None
            pause = True
            with pytest.raises(DeliveryError) as paused:
                await sender.send(notification, 'feishu')
            assert paused.value.code == 'tenant_paused' and paused.value.retryable
            assert len(sent) == 2
            assert await sender.available(who) == []
            pause = False
            await p.store.update_tenant(tenant)
            deactivate = True
            with pytest.raises(DeliveryError):
                await sender.send(notification,'feishu')
            assert len(sent) == 2
        finally:
            await sender.close()

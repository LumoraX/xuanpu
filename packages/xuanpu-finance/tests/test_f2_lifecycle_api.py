"""回调与已发送核对独立于员工入口和模型，停用也不得遗失真实结果。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from xuanpu_finance.api import create_app
from xuanpu_finance.settlement.provider_models import ProviderConfig
from xuanpu_finance.settlement.providers import ProviderRegistry

from .test_f2_api import Store


class NoEmployeeAuth:
    """供应商回调不能伪装员工，也不要求员工目录在线。"""

    async def authenticate(self, request: Any) -> Any:
        """被调用就表示错误接入了员工认证。"""
        raise AssertionError("callback reached employee authentication")


def config() -> ProviderConfig:
    """凭据值由registry注入；配置路径不会读宿主内容。"""
    return ProviderConfig(
        tenant_id="t",
        legal_entity_id="co",
        provider="demo",
        configuration_version=1,
        adapter_version="1",
        base_url="http://127.0.0.1:9000",
        token_file=Path("token"),
        callback_secret_file=Path("callback"),
        callback_key_id="k",
        simulation=True,
        supports_idempotency=True,
        supports_query=True,
    )


@pytest.mark.parametrize("mode", ["enabled", "draining", "read_only", "disabled"])
async def test_signed_callback_queues_query_in_all_modes(mode: str) -> None:
    """实际原字节HMAC在业务停用时仍核验并持久接收，不直接推进成功。"""
    store = Store()
    raw = json.dumps(
        {
            "tenant_id": "t",
            "legal_entity_id": "co",
            "provider": "demo",
            "configuration_version": 1,
            "simulation": True,
            "external_request_id": "request1",
        }
    ).encode()
    stamp = str(int(time.time()))
    headers = {
        "X-Finance-Timestamp": stamp,
        "X-Finance-Event-Id": "event1",
        "X-Finance-Key-Id": "k",
        "X-Finance-Signature": "sha256="
        + hmac.new(b"secret", stamp.encode() + b".event1." + raw, hashlib.sha256).hexdigest(),
    }
    async with httpx.AsyncClient() as upstream:
        registry = ProviderRegistry(
            [config()],
            upstream,
            tokens={("demo", 1): "token"},
            callback_secrets={("demo", 1): "secret"},
        )
        app = create_app(
            store=store,
            auth=NoEmployeeAuth(),
            mode=mode,
            start_worker=False,
            settlement_providers=registry,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://finance"
        ) as client:
            response = await client.post(
                "/v1/provider-callbacks/demo/1", content=raw, headers=headers
            )
            tampered = await client.post(
                "/v1/provider-callbacks/demo/1", content=raw + b" ", headers=headers
            )
    assert response.status_code == 202 and response.json() == {"accepted": True}, response.text
    assert tampered.status_code == 401
    assert len(store.calls) == 1 and store.calls[0][-1] == raw


class Worker:
    """观察应用传入的发送许可，保持任务可以取消。"""

    def __init__(self) -> None:
        """记录实际调用模式。"""
        self.flags: list[bool] = []
        self.called = asyncio.Event()

    async def run_once(self, *, allow_dispatch: bool = True) -> bool:
        """表示本轮无任务，让应用进入有界轮询。"""
        self.flags.append(allow_dispatch)
        self.called.set()
        return False


@pytest.mark.parametrize("mode", ["enabled", "draining", "read_only", "disabled"])
async def test_background_query_survives_disabled_modes(mode: str) -> None:
    """停用禁止新dispatch，但后台查询仍运行；退出后任务真正结束。"""
    worker = Worker()
    app = create_app(store=Store(), auth=NoEmployeeAuth(), mode=mode, settlement_worker=worker)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(worker.called.wait(), 2)
        task = app.state.settlement_task
        assert not task.done()
    assert worker.flags == [mode == "enabled"]
    assert task.done()

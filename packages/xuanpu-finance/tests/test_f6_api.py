"""F6签名只读API报告真实运行态并保持reviewer授权。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.objects import EncryptedLocalObjects

from .test_f0_e2e import SignedClient, app_for
from .test_f5_domain import setup_funding
from .test_worker import Gateway

if TYPE_CHECKING:
    from pathlib import Path


async def test_readiness_signed_api_runtime_and_reviewer(store: Any, tmp_path: Path) -> None:
    """当前岗位与实时故障独立反映；任何GET不能产生执行许可。"""
    await setup_funding(store)
    key = Ed25519PrivateKey.generate()
    app = app_for(store, EncryptedLocalObjects(tmp_path, b"k" * 32), Gateway({}), key)
    app.state.mode = "read_only"
    app.state.instance_lock_lost = True
    app.state.worker_error = "private-exception"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://finance"
    ) as client:
        signed = SignedClient(client, key)
        response = await signed.call(
            "GET",
            "/v1/finance/readiness",
            tenant="t",
            user="payer",
            params={"legal_entity_id": "e"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["runtime"]["mode"] == "read_only"
        assert response.json()["runtime"]["instance_lock_lost"] is True
        assert response.json()["runtime"]["worker"] == "failed"
        assert "private" not in response.text
        denied = await signed.call(
            "GET",
            "/v1/finance/readiness",
            tenant="t",
            user="owner",
            params={"legal_entity_id": "e"},
        )
        assert denied.status_code == 403

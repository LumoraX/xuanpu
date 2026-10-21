"""真实签名、ASGI、独立 PostgreSQL 与加密原件的 F0 联调。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.api import create_app
from xuanpu_finance.auth import FinanceAuthenticator
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.storage import FinanceStore
from xuanpu_spec.call_context import request_fingerprint

from .test_parser import SAMPLE
from .test_storage import WHO, prepare
from .test_worker import Gateway, ref

if TYPE_CHECKING:
    from pathlib import Path


class SignedClient:
    """使用真实密钥按最终请求签名，不绕过接收方鉴权。"""

    def __init__(self, client: httpx.AsyncClient, key: Ed25519PrivateKey) -> None:
        """持有独立测试服务连接与签名私钥。"""
        self.client, self.key = client, key

    async def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, str] | None = None,
        idem: str = "",
        user: str = WHO.user_id,
    ) -> httpx.Response:
        """精确绑定查询参数、JSON 与幂等键。"""
        content_type = "application/json" if body is not None else ""
        now = int(time.time())
        claims = {
            "iss": "platform",
            "aud": "finance",
            "sub": user,
            "tenant_id": WHO.tenant_id,
            "session_id": "session",
            "call_id": uuid4().hex,
            "iat": now,
            "exp": now + 60,
            "jti": uuid4().hex,
            "request_sha256": request_fingerprint(
                method, path, list((params or {}).items()), body, idem, content_type
            ),
        }
        headers = {
            "Authorization": "Bearer test-service",
            "X-Xuanpu-Context": jwt.encode(
                claims, self.key, algorithm="EdDSA", headers={"kid": "test"}
            ),
        }
        if idem:
            headers["Idempotency-Key"] = idem
        if content_type:
            headers["Content-Type"] = content_type
        return await self.client.request(method, path, json=body, params=params, headers=headers)


def app_for(
    store: FinanceStore, objects: EncryptedLocalObjects, gateway: Gateway, key: Ed25519PrivateKey
) -> Any:
    """重建服务只复用独立持久存储，网关是显式测试边界。"""
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    auth = FinanceAuthenticator(
        service_token="test-service",
        issuer="platform",
        audience="finance",
        public_keys={"test": public},
        store=store,
        gateway=gateway,
    )
    return create_app(
        store=store,
        auth=auth,
        worker=FinanceWorker(store, objects, gateway, synthetic=True),
        start_worker=False,
    )


async def test_signed_http_restart_hitl_and_original_independence(
    store: FinanceStore, tmp_path: Path
) -> None:
    """202落库、补件跨服务重建、权限撤销和平台源删除后副本保留。"""
    await prepare(store)
    pdf = b"%PDF-1.4 incomplete"
    gateway = Gateway({"pdf": pdf, "xml": SAMPLE})
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    key = Ed25519PrivateKey.generate()
    app = app_for(store, objects, gateway, key)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://finance"
    ) as c:
        signed = SignedClient(c, key)
        body = {"legal_entity_id": "company-a", "files": [ref("pdf", pdf).model_dump()]}
        accepted = await signed.call("POST", "/v1/finance/invoice-imports", body=body, idem="first")
        assert accepted.status_code == 202, accepted.text
        replay = await signed.call("POST", "/v1/finance/invoice-imports", body=body, idem="first")
        assert replay.json()["id"] == accepted.json()["id"]
        await app.state.worker.run_once()
        op = await signed.call("GET", f"/v1/finance/operations/{accepted.json()['id']}")
        assert op.json()["summary"]["awaiting_original"] == 1
        source = op.json()["items"][0]["exception_id"]
    restored = FinanceStore(store.pool)
    restarted = app_for(restored, objects, gateway, key)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(restarted), base_url="https://finance"
    ) as c:
        signed = SignedClient(c, key)
        pending = await signed.call(
            "GET", "/v1/finance/exceptions", params={"legal_entity_id": "company-a"}
        )
        assert pending.json()["items"][0]["id"] == source
        body = {
            "legal_entity_id": "company-a",
            "files": [ref("xml", SAMPLE).model_dump()],
            "source_exception_id": source,
            "expected_exception_version": 1,
        }
        accepted = await signed.call(
            "POST", "/v1/finance/invoice-imports", body=body, idem="supplement"
        )
        assert accepted.status_code == 202, accepted.text
        await restarted.state.worker.run_once()
        pending = await signed.call(
            "GET", "/v1/finance/exceptions", params={"legal_entity_id": "company-a"}
        )
        assert pending.json()["items"][0]["status"] == "resolved"
        listed = await signed.call(
            "GET", "/v1/finance/invoices", params={"legal_entity_id": "company-a"}
        )
        invoice = listed.json()["items"][0]
        assert invoice["verification_status"] == "not_checked"
        evidence = await signed.call("GET", f"/v1/finance/invoices/{invoice['id']}/evidence")
        assert len(evidence.json()["evidence"]) == 2
        await store.grant(WHO.tenant_id, "outsider", "company-a")
        denied = await signed.call("GET", f"/v1/finance/invoices/{invoice['id']}", user="outsider")
        assert denied.status_code == 404
        gateway.payloads.clear()
        records = await store.pool.fetch("SELECT object_key FROM finance_receipt ORDER BY id")
        assert {await objects.get(record["object_key"]) for record in records} == {pdf, SAMPLE}
        gateway.enabled = False
        denied = await signed.call(
            "GET", "/v1/finance/invoices", params={"legal_entity_id": "company-a"}
        )
        assert denied.status_code == 403

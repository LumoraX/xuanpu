"""独立真实PG、请求签名、实际TCP渠道与人工计划的F2隔离联调。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.settlement.models import BusinessSnapshot, PayeeAccount, SettlementPolicy
from xuanpu_finance.settlement.provider_models import ProviderConfig
from xuanpu_finance.settlement.providers import ProviderRegistry
from xuanpu_finance.settlement.worker import SettlementWorker
from xuanpu_finance.storage.common import decode

from .test_f0_e2e import SignedClient, app_for
from .test_f2_domain import approved_payable, provider_result, ref
from .test_worker import Gateway

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


class IsolatedProvider:
    """仅在loopback测试端口受理固定请求，并保存实际发送次数。"""

    def __init__(self) -> None:
        """回显最终真实凭证，付款先受理，查询才成功。"""
        self.requests: dict[str, dict[str, Any]] = {}
        self.posts: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.bad_payment = False

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """读取有界完整HTTP请求并响应，避免mock transport冒充实际网络。"""
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            method, path, _ = lines[0].split(" ")
            headers = {
                name.lower(): value
                for name, value in (line.split(": ", 1) for line in lines[1:] if line)
            }
            assert headers["authorization"] == "Bearer isolated-token"
            if method == "POST":
                assert path == "/v1/requests"
                body = json.loads(await reader.readexactly(int(headers["content-length"])))
                assert headers["idempotency-key"] == body["external_request_id"]
                assert body["simulation"] is True
                self.requests[body["external_request_id"]] = body
                self.posts.append(body)
                result = provider_result(
                    body, "accepted" if body["action"] == "execute_payment" else "succeeded"
                )
                if self.bad_payment and body["action"] == "execute_payment":
                    result.update(status="succeeded", amount="999.00")
            else:
                assert method == "GET" and path.startswith("/v1/requests/")
                request_id = path.rsplit("/", 1)[-1]
                self.queries.append(request_id)
                result = provider_result(self.requests[request_id])
            raw = json.dumps(result).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                + str(len(raw)).encode()
                + b"\r\n\r\n"
                + raw
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@asynccontextmanager
async def provider_server() -> AsyncIterator[tuple[IsolatedProvider, str]]:
    """只创建随机loopback监听器并在用例结束关闭。"""
    provider = IsolatedProvider()
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    try:
        yield provider, "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


async def bind_provider(store: FinanceStore, origin: str, tmp_path: Path) -> ProviderConfig:
    """以新配置版本绑定真实测试端口，不修改既有不可变渠道版本。"""
    policy = decode(
        await store.pool.fetchval(
            "SELECT data FROM finance_settlement_policy_revision WHERE version=1"
        )
    )
    policy.update(
        version=2, enabled=True, payment_configuration_version=2, posting_configuration_version=2
    )
    rows = await store.pool.fetch("SELECT data FROM finance_settlement_catalog_revision")
    documents, accounts = [], []
    for row in rows:
        data = decode(row["data"])
        if data["ref"]["kind"].endswith("_account"):
            accounts.append(PayeeAccount.model_validate(data))
        else:
            documents.append(BusinessSnapshot.model_validate(data))
    config = ProviderConfig(
        tenant_id="t",
        legal_entity_id="e",
        provider="test",
        configuration_version=2,
        adapter_version="http-v1",
        base_url=origin,
        token_file=tmp_path / "token",
        callback_secret_file=tmp_path / "callback",
        callback_key_id="key",
        simulation=True,
        supports_idempotency=True,
        supports_query=True,
    )
    await store.configure_settlements(
        [SettlementPolicy.model_validate(policy)],
        documents,
        accounts,
        [config.public_descriptor()],
        simulation=True,
    )
    return config


async def approved_http_plan(
    signed: SignedClient,
    user: str,
    target: dict[str, Any],
    action: str,
    amount: str,
    **parameters: Any,
) -> dict[str, Any]:
    """实际HTTP由经办提出、实际分派人批准，原经办才可执行。"""

    async def call(method: str, path: str, actor: str, **kwargs: Any) -> dict[str, Any]:
        """每次请求均使用真实签名和独立数据库防重放。"""
        response = await signed.call(method, path, tenant="t", user=actor, origin="human", **kwargs)
        assert response.is_success, response.text
        return response.json()

    body = {
        "action": action,
        "target_type": target.get("kind", "payable"),
        "target_id": target["id"],
        "expected_version": target["version"],
        "parameters": {"amount": amount, **parameters},
    }
    plan = await call(
        "POST", "/v1/finance/action-previews", user, body=body, idem=action + target["id"]
    )
    assert plan["executable"] is False
    rejected = await signed.call(
        "POST",
        "/v1/finance/action-executions",
        tenant="t",
        user=user,
        origin="agent",
        body={"plan_id": plan["id"], "plan_digest": plan["digest"]},
        idem="agent-" + plan["id"],
    )
    assert rejected.status_code == 403
    queue = await call("GET", "/v1/finance/review-queue", "a", params={"legal_entity_id": "e"})
    item = next(item for item in queue["items"] if item["target_id"] == plan["id"])
    review = await call(
        "POST",
        "/v1/finance/action-previews",
        "a",
        body={
            "action": "approve",
            "target_type": "action_plan",
            "target_id": plan["id"],
            "expected_version": 1,
            "parameters": {"workflow_step_id": item["workflow_step_id"]},
        },
        idem="approve-" + plan["id"],
    )
    await call(
        "POST",
        "/v1/finance/action-executions",
        "a",
        body={"plan_id": review["id"], "plan_digest": review["digest"]},
        idem="decision-" + plan["id"],
    )
    ready = await call("GET", "/v1/finance/action-plans/" + plan["id"], user)
    assert ready["digest"] == plan["digest"] and ready["executable"]
    return await call(
        "POST",
        "/v1/finance/action-executions",
        user,
        body={"plan_id": plan["id"], "plan_digest": plan["digest"]},
        idem="execute-" + plan["id"],
    )


async def assert_callback_queries_original_request(
    store: FinanceStore,
    payer: Any,
    doc: dict[str, Any],
    remote: IsolatedProvider,
    app: Any,
    client: httpx.AsyncClient,
    worker: SettlementWorker,
) -> None:
    """停发时验证真实HMAC回调只触发原请求查单，重复回调不会重复入库或付款。"""
    request = remote.posts[0]
    event = json.dumps({**request, "status": "succeeded"}).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"s" * 32, timestamp.encode() + b".event-1." + event, hashlib.sha256
    ).hexdigest()
    headers = {
        "X-Finance-Timestamp": timestamp,
        "X-Finance-Event-Id": "event-1",
        "X-Finance-Key-Id": "key",
        "X-Finance-Signature": "sha256=" + signature,
    }
    app.state.mode = "disabled"
    callback = await client.post("/v1/provider-callbacks/test/2", content=event, headers=headers)
    assert callback.status_code == 202, callback.text
    assert (await store.get_payable(payer, doc["id"]))["paid_amount"] == "0.00"
    await worker.run_once(allow_dispatch=False)
    assert (await store.get_payable(payer, doc["id"]))["paid_amount"] == "600.00"
    assert len(remote.posts) == 1 and remote.queries == [request["external_request_id"]]
    duplicate = await client.post("/v1/provider-callbacks/test/2", content=event, headers=headers)
    assert (
        duplicate.status_code == 202
        and await store.pool.fetchval("SELECT count(*) FROM finance_settlement_inbox") == 1
    )


async def test_signed_hitl_payment_posting_reversal_over_real_http(
    store: FinanceStore, tmp_path: Path
) -> None:
    """受理不等于成功；停发时回调只唤起查单；付款与凭证及冲回分别落库。"""
    _, payer, _, _, doc = await approved_payable(store)
    async with provider_server() as (remote, origin), httpx.AsyncClient(trust_env=False) as network:
        config = await bind_provider(store, origin, tmp_path)
        gateway = Gateway({})
        registry = ProviderRegistry(
            [config],
            network,
            tokens={("test", 2): "isolated-token"},
            callback_secrets={("test", 2): "s" * 32},
        )
        worker = SettlementWorker(store, registry, gateway)
        key = Ed25519PrivateKey.generate()
        app = app_for(store, EncryptedLocalObjects(tmp_path, b"k" * 32), gateway, key)
        app.state.settlement_providers = registry
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://finance"
        ) as client:
            signed = SignedClient(client, key)
            payment = await approved_http_plan(
                signed,
                "payer",
                doc,
                "execute_payment",
                "600.00",
                payee_ref=ref("supplier_account", "account"),
            )
            assert payment["status"] == "accepted"
            await worker.run_once()
            summary = await store.get_payable(payer, doc["id"])
            assert (
                summary["paid_amount"] == "0.00" and summary["payment_pending_amount"] == "600.00"
            )
            await assert_callback_queries_original_request(
                store, payer, doc, remote, app, client, worker
            )
            app.state.mode = "enabled"
            await approved_http_plan(
                signed, "accountant", doc, "submit_posting", "1000.00", accounting_period="2026-10"
            )
            await worker.run_once()
            posting = (await store.list_postings(payer, "e"))["items"][0]
            assert posting["status"] == "succeeded" and posting["voucher_id"]
            await approved_http_plan(
                signed,
                "accountant",
                {**posting, "kind": "posting"},
                "reverse_posting",
                "250.00",
                accounting_period="2026-10",
                reason="隔离测试冲回",
            )
            await worker.run_once()
            summary = await store.get_payable(payer, doc["id"])
            assert summary["posted_amount"] == "750.00" and summary["reversed_amount"] == "250.00"
            assert (
                len(remote.posts) == 3
                and remote.posts[2]["original_voucher_id"] == posting["voucher_id"]
            )
            assert await store.pool.fetchval("SELECT count(*) FROM finance_settlement_receipt") == 4


async def test_invalid_result_is_unknown_and_rebuilt_worker_only_queries(
    store: FinanceStore, tmp_path: Path
) -> None:
    """实际渠道回错金额时不认付款，重建worker也绝不第二次POST。"""
    _, payer, _, _, doc = await approved_payable(store)
    async with provider_server() as (remote, origin), httpx.AsyncClient(trust_env=False) as network:
        remote.bad_payment = True
        config = await bind_provider(store, origin, tmp_path)
        registry = ProviderRegistry(
            [config],
            network,
            tokens={("test", 2): "isolated-token"},
            callback_secrets={("test", 2): "s" * 32},
        )
        gateway, key = Gateway({}), Ed25519PrivateKey.generate()
        app = app_for(store, EncryptedLocalObjects(tmp_path, b"k" * 32), gateway, key)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://finance"
        ) as client:
            signed = SignedClient(client, key)
            operation = await approved_http_plan(
                signed,
                "payer",
                doc,
                "execute_payment",
                "600.00",
                payee_ref=ref("supplier_account", "account"),
            )
            await SettlementWorker(store, registry, gateway).run_once()
            current = await store.get_operation(payer, operation["id"])
            assert current["status"] == "outcome_unknown"
            assert (await store.get_payable(payer, doc["id"]))["payment_pending_amount"] == "600.00"
            raw = await store.pool.fetchval("SELECT raw FROM finance_settlement_receipt")
            assert json.loads(raw)["amount"] == "999.00"
            response = await signed.call(
                "POST",
                "/v1/finance/operations/" + operation["id"] + "/reconcile",
                tenant="t",
                user="payer",
                origin="human",
                body={"expected_version": current["version"]},
                idem="reconcile",
            )
            assert response.status_code == 202, response.text
            await SettlementWorker(store, registry, gateway).run_once(allow_dispatch=False)
            assert len(remote.posts) == 1 and len(remote.queries) == 1
            assert (await store.get_operation(payer, operation["id"]))["status"] == "succeeded"

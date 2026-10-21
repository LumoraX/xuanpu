"""F2新增十三项操作的实际签名HTTP响应与发布契约逐项对应。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.objects import EncryptedLocalObjects

from .test_contract import ResponseContract, convert, published_contract
from .test_expense_domain import execute
from .test_f0_e2e import SignedClient, app_for
from .test_f2_domain import action_plan, approved_payable, ref
from .test_worker import Gateway


async def assert_draft_and_catalog_contracts(
    call: Any, payer: Any, document: dict[str, Any]
) -> None:
    """分别验证应付新建、列表、修订以及真实受控目录和执行选项的响应契约。"""
    draft = {
        "legal_entity_id": "e",
        "supplier_ref": ref("supplier", "supplier"),
        "amount": "10.00",
        "due_date": "2026-11-01",
        "business_refs": [ref("contract", "contract")],
        "reason": "契约验证",
    }
    created = await call("POST", "/v1/finance/payables", body=draft, idem="contract-create")
    await call("GET", "/v1/finance/payables", params={"legal_entity_id": "e"})
    path = "/v1/finance/payables/" + created["id"]
    await call("GET", "/v1/finance/payables/{id}", path)
    await call(
        "PUT",
        "/v1/finance/payables/{id}",
        path,
        body={"expected_version": created["version"], "draft": {**draft, "reason": "已修订"}},
        idem="contract-update",
    )
    await call(
        "GET",
        "/v1/finance/settlement-options",
        user=payer.user_id,
        params={"legal_entity_id": "e"},
    )
    await call(
        "GET",
        "/v1/finance/catalog/business-documents",
        cat=True,
        params={"legal_entity_id": "e", "kind": "supplier"},
    )
    await call(
        "GET",
        "/v1/finance/catalog/payee-accounts",
        cat=True,
        user=payer.user_id,
        params={
            "legal_entity_id": "e",
            "document_kind": "payable",
            "document_id": document["id"],
        },
    )


async def assert_external_record_contracts(
    call: Any,
    store: Any,
    actor: Any,
    approver: Any,
    document: dict[str, Any],
    action: str,
    kind: str,
) -> None:
    """保留真实已发送事实，分别验证付款、凭证和人工原请求核对的响应契约。"""
    plan = await action_plan(store, actor, approver, document, action)
    operation = await execute(store, actor, plan)
    rows = await call(
        "GET",
        "/v1/finance/" + kind,
        user=actor.user_id,
        params={"legal_entity_id": "e", "document_id": document["id"]},
    )
    await call(
        "GET",
        "/v1/finance/" + kind + "/{id}",
        "/v1/finance/" + kind + "/" + rows["items"][0]["id"],
        user=actor.user_id,
    )
    job = await store.claim_settlement()
    await store.prepare_settlement_dispatch(job)
    await store.release_settlement(job)
    current = await store.get_operation(actor, operation["id"])
    await call(
        "POST",
        "/v1/finance/operations/{id}/reconcile",
        "/v1/finance/operations/" + operation["id"] + "/reconcile",
        user=actor.user_id,
        body={"expected_version": current["version"]},
        idem="contract-query-" + kind,
    )
    # 保留该请求dispatching事实；本例只核验HTTP响应，不伪造渠道结果。
    await store.pool.execute(
        "UPDATE finance_settlement_operation SET next_query_at=now()+interval '1 day',query_requested=false WHERE id=$1",
        operation["id"],
    )


async def test_all_thirteen_f2_http_responses_match_published_contract(
    store: Any, tmp_path: Path
) -> None:
    """新读写/目录与人工核对均用真实验签和PG，禁止样例JSON代替实际响应。"""
    owner, payer, accountant, approver, document = await approved_payable(store)
    checked: set[str] = set()
    finance = ResponseContract(published_contract(), checked)
    catalog = ResponseContract(
        convert(
            yaml.safe_load(
                (
                    Path(__file__).resolve().parents[3] / "examples/finance/apis/catalog.yaml"
                ).read_text()
            )
        ),
        checked,
    )
    key = Ed25519PrivateKey.generate()
    app = app_for(store, EncryptedLocalObjects(tmp_path, b"k" * 32), Gateway({}), key)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://finance"
    ) as client:
        signed = SignedClient(client, key)

        async def call(
            method: str,
            route: str,
            path: str | None = None,
            *,
            user: str = owner.user_id,
            cat: bool = False,
            **kwargs: Any,
        ) -> dict[str, Any]:
            """目录使用各自完整组件，签名来自明确隔离用户。"""
            return await (catalog if cat else finance)(
                signed,
                method,
                route,
                path or route,
                tenant="t",
                user=user,
                origin="human",
                **kwargs,
            )

        await assert_draft_and_catalog_contracts(call, payer, document)
        for actor, action, kind in (
            (payer, "execute_payment", "payments"),
            (accountant, "submit_posting", "postings"),
        ):
            await assert_external_record_contracts(
                call, store, actor, approver, document, action, kind
            )
        await call(
            "GET", "/v1/finance/action-plans", user=payer.user_id, params={"legal_entity_id": "e"}
        )
    assert len(checked) == 13, checked

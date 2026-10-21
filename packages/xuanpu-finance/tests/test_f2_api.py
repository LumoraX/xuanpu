"""F2公开路由与独立供应商回调的边界，数据库事务另由真实PG验证。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from xuanpu_finance.api import create_app

from .test_f1_api import Auth


class Store:
    """仅记录路由参数，不把本测试当外部支付验收。"""

    def __init__(self) -> None:
        """建立已受理参数列表。"""
        self.calls: list[Any] = []

    async def create_payable(self, who: Any, body: Any, key: str) -> dict[str, Any]:
        """返回领域受理的应付草稿。"""
        self.calls.append((who, body, key))
        return {"id": "p1", "version": 1, "status": "draft", "total": body.amount}

    async def list_payments(self, who: Any, entity: str, **kwargs: Any) -> dict[str, Any]:
        """独立total不能以当前页条数替代。"""
        self.calls.append((who, entity, kwargs))
        return {"items": [], "total": 9, "next_cursor": None}

    async def reconcile_operation(
        self, who: Any, id: str, expected_version: int, key: str
    ) -> dict[str, Any]:
        """人工只能请求只读回查，不提供成功状态参数。"""
        self.calls.append((who, id, expected_version, key))
        return {"id": id, "status": "outcome_unknown"}

    async def accept_provider_event(
        self, provider: str, version: int, event: str, digest: str, raw: bytes
    ) -> dict[str, Any]:
        """回调仅持久保存并排同请求查单。"""
        self.calls.append((provider, version, event, digest, raw))
        return {"accepted": True}


def draft() -> dict[str, Any]:
    """完整BusinessRef，拒绝只有聊天自报外部单号。"""
    ref = {
        "kind": "supplier",
        "source": "internal",
        "id": "supplier1",
        "version": "1",
        "sha256": "a" * 64,
    }
    return {
        "legal_entity_id": "co1",
        "supplier_ref": ref,
        "amount": "10.00",
        "due_date": "2026-10-21",
        "reason": "服务采购",
        "business_refs": [{**ref, "kind": "contract", "id": "contract1"}],
    }


async def test_payable_route_accepts_strict_draft() -> None:
    """实际201路由传入当前身份与完整严格模型。"""
    store = Store()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(store=store, auth=Auth(), start_worker=False)),
        base_url="http://finance",
    ) as client:
        response = await client.post(
            "/v1/finance/payables", headers={"Idempotency-Key": "draft"}, json=draft()
        )
    assert response.status_code == 201, response.text
    assert store.calls[0][1].amount == "10.00"


@pytest.mark.parametrize("extra", [{"owner_id": "other"}, {"status": "paid"}, {"amount": 10.0}])
async def test_payable_route_rejects_identity_state_and_float(extra: dict[str, Any]) -> None:
    """模型不能自报审批结果和金额类型。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(store=Store(), auth=Auth(), start_worker=False)),
        base_url="http://finance",
    ) as client:
        response = await client.post(
            "/v1/finance/payables", headers={"Idempotency-Key": "draft"}, json={**draft(), **extra}
        )
    assert response.status_code == 422, response.text


async def test_payment_list_keeps_filtered_total() -> None:
    """页面恢复结果依赖服务端查询，不依赖模型记忆。"""
    store = Store()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(store=store, auth=Auth(), start_worker=False)),
        base_url="http://finance",
    ) as client:
        response = await client.get(
            "/v1/finance/payments", params={"legal_entity_id": "co1", "document_id": "c1"}
        )
    assert response.status_code == 200 and response.json()["total"] == 9
    assert store.calls[0][2]["document_id"] == "c1"


@pytest.mark.parametrize("origin", ["human", "agent"])
async def test_reconcile_requires_human_without_success_override(origin: str) -> None:
    """核对由真人触发，只排只读查询，不接受状态修改。"""
    store = Store()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            create_app(store=store, auth=Auth(origin), start_worker=False)
        ),
        base_url="http://finance",
    ) as client:
        response = await client.post(
            "/v1/finance/operations/f2op_one/reconcile",
            headers={"Idempotency-Key": "reconcile"},
            json={"expected_version": 1},
        )
    assert response.status_code == (202 if origin == "human" else 403), response.text
    assert bool(store.calls) == (origin == "human")

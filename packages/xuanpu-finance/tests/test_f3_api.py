"""F3公开路由严格传递合规版本与真实人工来源。"""
from __future__ import annotations

from typing import Any

import httpx
from xuanpu_finance.api import create_app

from .test_f1_api import Auth


class Store:
    """仅检查路由参数，不替代独立PG领域证据。"""

    def __init__(self) -> None:
        """记录受理命令。"""
        self.calls: list[Any] = []

    async def verify_invoice(self, who: Any, id: str, version: int, key: str) -> dict[str, Any]:
        """返回查验受理，版本来自显式请求。"""
        self.calls.append((who, id, version, key))
        return {"id": "f3op_test", "status": "accepted"}

    async def preview_finance_action(self, who: Any, body: Any, key: str) -> dict[str, Any]:
        """F3 typed预检不能由F1参数校验吞掉。"""
        self.calls.append((who, body, key))
        return {"id": "f3plan_test", "action": body.action}


async def test_verify_route_uses_compliance_version() -> None:
    """agent可请求实际查验，但不得直接指定passed。"""
    store = Store()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(
        store=store, auth=Auth("agent"), start_worker=False)), base_url="http://finance") as client:
        response = await client.post("/v1/finance/invoices/i/verifications",
            json={"expected_version": 3},headers={"Idempotency-Key":"check"})
        assert response.status_code == 202
        forged = await client.post("/v1/finance/invoices/i/verifications",
            json={"expected_version": 3,"verification_status":"passed"},headers={"Idempotency-Key":"bad"})
        assert forged.status_code == 422
    assert store.calls[0][2] == 3


async def test_tax_preview_accepts_typed_target() -> None:
    """用途确认目标是发票，不能强塞报销单。"""
    store = Store()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(
        store=store, auth=Auth(), start_worker=False)), base_url="http://finance") as client:
        response = await client.post("/v1/finance/action-previews", headers={"Idempotency-Key":"plan"},
            json=dict(action="confirm_usage",target_type="invoice",target_id="i",expected_version=3,
                      parameters={"accounting_period":"2026-10"}))
        assert response.status_code == 201, response.text
    assert store.calls[0][1].target_type == "invoice"


async def test_compliance_worker_failure_stops_new_acceptance() -> None:
    """第三执行器故障须同样关闭受理，不能继续堆积新任务。"""
    store = Store()
    app = create_app(store=store,auth=Auth(),start_worker=False)
    app.state.compliance_worker_error = "ReadTimeout"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://finance") as client:
        response = await client.post("/v1/finance/invoices/i/verifications",
            json={"expected_version":1},headers={"Idempotency-Key":"paused"})
    assert response.status_code == 503
    assert store.calls == []

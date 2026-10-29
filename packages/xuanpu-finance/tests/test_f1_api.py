"""F1路由只将严格命令交给独立财务领域，不用内存替代领域事务验收。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from xuanpu_finance.api import create_app
from xuanpu_finance.models import FinancePrincipal


class Auth:
    """路由测试明确注入来源，真实签名另测。"""

    def __init__(self, origin: str = "human") -> None:
        """选择测试入口来源。"""
        self.origin = origin

    async def authenticate(self, request: Any) -> FinancePrincipal:
        """提供测试已验证员工，不伪装成实际认证验收。"""
        return FinancePrincipal("acme", "alice", origin=self.origin)


class Store:
    """只记录路由调用，PG领域由独立用例验证。"""

    def __init__(self) -> None:
        """保存已提交命令。"""
        self.calls: list[Any] = []

    async def create_expense_claim(self, who: Any, body: Any, key: str) -> dict[str, Any]:
        """返回实际领域契约的草稿标识。"""
        self.calls.append((who, body, key))
        return {"id": "claim1", "version": 1, "status": "draft", "total": "10.00"}

    async def list_expense_claims(self, who: Any, entity: str, **kwargs: Any) -> dict[str, Any]:
        """列表恢复使用服务权限范围，不把页长当total。"""
        self.calls.append((who, entity, kwargs))
        return {"items": [], "total": 7, "next_cursor": "next"}


def draft() -> dict[str, Any]:
    """最小有效业务事实，金额保持字符串。"""
    return {
        "legal_entity_id": "company-a",
        "reason": "客户拜访交通",
        "lines": [
            {
                "category_id": "travel",
                "amount": "10.00",
                "occurred_on": "2026-10-21",
                "reason": "地铁",
            }
        ],
    }


async def test_create_claim_has_real_route_and_strict_body() -> None:
    """草稿路由返回201，不把归集导入当报销提交。"""
    store = Store()
    app = create_app(store=store, auth=Auth(), start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://finance"
    ) as c:
        response = await c.post(
            "/v1/finance/expense-claims", headers={"Idempotency-Key": "create"}, json=draft()
        )
    assert response.status_code == 201, response.text
    assert store.calls[0][1].lines[0].amount == "10.00"


async def test_claim_list_restores_filtered_total() -> None:
    """刷新后通过列表恢复，不要求员工记住ID。"""
    store = Store()
    app = create_app(store=store, auth=Auth(), start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://finance"
    ) as c:
        response = await c.get(
            "/v1/finance/expense-claims", params={"legal_entity_id": "company-a", "status": "draft"}
        )
    assert response.status_code == 200 and response.json()["total"] == 7
    assert store.calls[0][2]["status"] == "draft"


@pytest.mark.parametrize("origin", ["agent"])
async def test_agent_cannot_execute_business_action(origin: str) -> None:
    """通用工具已放行也不构成人工业务决定。"""
    app = create_app(store=Store(), auth=Auth(origin), start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://finance"
    ) as c:
        response = await c.post(
            "/v1/finance/action-executions",
            headers={"Idempotency-Key": "execute"},
            json={"plan_id": "plan1", "plan_digest": "a" * 64},
        )
    assert response.status_code == 403 and response.json()["code"] == "HUMAN_REQUIRED"


@pytest.mark.parametrize(
    "extra", [{"owner_id": "boss"}, {"status": "approved"}, {"origin": "human"}]
)
async def test_claim_rejects_identity_or_approval_in_body(extra: Any) -> None:
    """模型不能靠额外字段声明审批人或成功状态。"""
    store = Store()
    app = create_app(store=store, auth=Auth(), start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://finance"
    ) as c:
        response = await c.post(
            "/v1/finance/expense-claims",
            headers={"Idempotency-Key": "create"},
            json={**draft(), **extra},
        )
    assert response.status_code == 422 and not store.calls


@pytest.mark.parametrize("mode", ["draining", "read_only", "disabled"])
async def test_f1_create_obeys_module_switch(mode: str) -> None:
    """独立存储保留，停用/排空/只读不能接受新报销。"""
    store = Store()
    app = create_app(store=store, auth=Auth(), mode=mode, start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://finance"
    ) as c:
        response = await c.post(
            "/v1/finance/expense-claims", headers={"Idempotency-Key": "create"}, json=draft()
        )
    assert response.status_code in (409, 503) and not store.calls

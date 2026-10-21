"""财务 API 的持久受理、模式和严格输入边界。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from xuanpu_finance.api import create_app
from xuanpu_finance.models import FinancePrincipal


class TestAuth:
    """此组只测路由，真实验签在 test_auth 中覆盖。"""

    async def authenticate(self, request: Any) -> FinancePrincipal:
        """提供明确经过边界验证的测试主体。"""
        return FinancePrincipal("acme", "alice")


class Store:
    """验证路由交给持久服务的语义，不冒充数据库事务测试。"""

    def __init__(self) -> None:
        """记录是否受理。"""
        self.accepted: list[Any] = []

    async def accept_import(
        self, who: Any, company: str, files: Any, key: str, **kwargs: Any
    ) -> dict[str, Any]:
        """持久层返回后才提供受理标识。"""
        self.accepted.append((who, company, files, key, kwargs))
        return {"id": "operation1", "version": 1, "status": "accepted"}


async def test_import_returns_persisted_operation() -> None:
    """202 必须来自服务实际受理结果。"""
    store = Store()
    app = create_app(store=store, auth=TestAuth(), start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://finance"
    ) as client:
        response = await client.post(
            "/v1/finance/invoice-imports",
            headers={"Idempotency-Key": "one"},
            json={
                "legal_entity_id": "co1",
                "files": [{"file_id": "f1", "revision": 1, "sha256": "a" * 64}],
            },
        )
    assert response.status_code == 202 and response.json()["id"] == "operation1"
    assert store.accepted[0][0].user_id == "alice"


@pytest.mark.parametrize("mode", ["draining", "read_only", "disabled"])
async def test_module_mode_blocks_new_imports(mode: str) -> None:
    """停用不靠模型遵守一句提示。"""
    store = Store()
    app = create_app(store=store, auth=TestAuth(), mode=mode, start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://finance"
    ) as client:
        response = await client.post(
            "/v1/finance/invoice-imports",
            headers={"Idempotency-Key": "one"},
            json={
                "legal_entity_id": "co1",
                "files": [{"file_id": "f1", "revision": 1, "sha256": "a" * 64}],
            },
        )
    assert response.status_code in (409, 503) and not store.accepted


@pytest.mark.parametrize(
    "extra", [{"user_id": "boss"}, {"status": "passed"}, {"object_key": "secret"}]
)
async def test_import_cannot_choose_identity_or_state(extra: dict[str, str]) -> None:
    """额外授权与原始存储路径字段应被拒绝。"""
    store = Store()
    app = create_app(store=store, auth=TestAuth(), start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://finance"
    ) as client:
        response = await client.post(
            "/v1/finance/invoice-imports",
            headers={"Idempotency-Key": "one"},
            json={
                "legal_entity_id": "co1",
                "files": [{"file_id": "f1", "revision": 1, "sha256": "a" * 64}],
                **extra,
            },
        )
    assert response.status_code == 422 and not store.accepted


async def test_missing_idempotency_key_is_rejected() -> None:
    """写接口不能绕过幂等控制。"""
    store = Store()
    app = create_app(store=store, auth=TestAuth(), start_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://finance"
    ) as client:
        response = await client.post(
            "/v1/finance/invoice-imports", json={"legal_entity_id": "co1", "files": []}
        )
    assert response.status_code == 422 and not store.accepted

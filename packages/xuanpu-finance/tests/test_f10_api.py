"""F10签名API、实时任务状态及健康故障的脱敏边界。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.api import create_app
from xuanpu_finance.objects import EncryptedLocalObjects

from .test_f0_e2e import SignedClient, app_for
from .test_f10_readiness import ready
from .test_package import validate_schema
from .test_worker import Gateway

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore

PATH = "/v1/finance/internal-records-readiness"


async def test_signed_internal_report_runtime_and_modes(
    store: FinanceStore, tmp_path: Path
) -> None:
    """运行中的真实任务可核对，只读合法；故障、退出、外部装配和锁丢失阻断。"""
    await ready(store)
    key = Ed25519PrivateKey.generate()
    general = app_for(store, EncryptedLocalObjects(tmp_path, b"k" * 32), Gateway({}), key)
    app = create_app(
        store=store,
        auth=general.state.auth,
        profile="internal_records",
        mode="read_only",
        start_worker=False,
    )

    async def idle() -> None:
        """持有真实未结束任务直到测试清理。"""
        await asyncio.Event().wait()

    task = asyncio.create_task(idle())
    app.state.worker_task = app.state.notification_task = task
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://finance"
        ) as client:
            signed = SignedClient(client, key)

            async def report() -> httpx.Response:
                """每次用真实签名重新读取当前运行态。"""
                return await signed.call(
                    "GET", PATH, tenant="t", user="reviewer", params={"legal_entity_id": "e"}
                )

            response = await report()
            assert response.status_code == 200, response.text
            data = response.json()
            validate_schema("InternalRecordsReadiness", data)
            assert set(data) == {
                "legal_entity_id",
                "profile",
                "generated_at",
                "status",
                "production_status",
                "checks",
                "versions",
                "configuration_digest",
                "external_verifications",
                "runtime",
            }
            assert data["status"] == "not_verified"
            assert data["runtime"] == {
                "mode": "read_only",
                "instance_lock_lost": False,
                "receipt_worker": "running",
                "notification_worker": "running",
                "external_workers": "absent",
            }
            digest = data["configuration_digest"]
            for name, value in [
                ("instance_lock_lost", True),
                ("notification_worker_error", "secret diagnostic"),
                ("settlement_providers", object()),
            ]:
                before = getattr(app.state, name)
                setattr(app.state, name, value)
                failed = await report()
                assert failed.json()["status"] == "blocked"
                assert failed.json()["configuration_digest"] == digest
                assert "secret diagnostic" not in failed.text
                setattr(app.state, name, before)
            app.state.worker_task = None
            assert (await report()).json()["runtime"]["receipt_worker"] == "not_started"
            assert (await report()).json()["status"] == "blocked"
            app.state.mode = "disabled"
            assert (await report()).status_code == 503
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_general_forbidden_company_reviewer_required(
    store: FinanceStore, tmp_path: Path
) -> None:
    """通用profile明确403，内部collector、审批人及跨公司身份不可读。"""
    await ready(store)
    key = Ed25519PrivateKey.generate()
    general = app_for(store, EncryptedLocalObjects(tmp_path, b"k" * 32), Gateway({}), key)
    internal = create_app(
        store=store, auth=general.state.auth, profile="internal_records", start_worker=False
    )
    for app, user, entity, code in [
        (general, "reviewer", "e", "INTERNAL_RECORDS_ONLY"),
        (internal, "owner", "e", "READINESS_FORBIDDEN"),
        (internal, "a", "e", "READINESS_FORBIDDEN"),
        (internal, "reviewer", "other", "READINESS_FORBIDDEN"),
    ]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://finance"
        ) as client:
            result = await SignedClient(client, key).call(
                "GET", PATH, tenant="t", user=user, params={"legal_entity_id": entity}
            )
            assert result.status_code == 403, result.text
            assert result.json()["code"] == code


@pytest.mark.parametrize(
    "name,value", [("notification_worker_error", "private failure"), ("instance_lock_lost", True)]
)
async def test_health_reports_new_fault_without_diagnostics(
    store: FinanceStore, tmp_path: Path, name: str, value: Any
) -> None:
    """健康入口对站内通知故障及实例锁丢失返回503，拒绝诊断回显。"""
    app = app_for(
        store, EncryptedLocalObjects(tmp_path, b"k" * 32), Gateway({}), Ed25519PrivateKey.generate()
    )
    setattr(app.state, name, value)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://finance"
    ) as client:
        result = await client.get("/health")
    assert result.status_code == 503
    assert result.json() == {"status": "unavailable", "mode": "enabled"}


async def test_exited_task_and_worker_error_are_failed(store: FinanceStore, tmp_path: Path) -> None:
    """已正常退出或异常退出任务均不可标成running，错误正文不外泄。"""
    from xuanpu_finance.routes_f10 import internal_runtime

    app = app_for(
        store, EncryptedLocalObjects(tmp_path, b"k" * 32), Gateway({}), Ed25519PrivateKey.generate()
    )

    async def ended() -> None:
        """真实任务立即退出。"""
        return None

    task = asyncio.create_task(ended())
    await task
    app.state.worker_task = task
    assert internal_runtime(app.state)["receipt_worker"] == "failed"
    app.state.notification_worker_error = "private failure"
    assert internal_runtime(app.state)["notification_worker"] == "failed"
    app.state.compliance_providers = {}
    assert internal_runtime(app.state)["external_workers"] == "present"

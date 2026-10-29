"""F12以实际内部记录制度、PDF、存储和可观察检查点构造收件场景。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.storage.common import decode

from .test_f7_records import configured, source
from .test_f8_materials import PdfGateway
from .test_worker import ref

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage import FinanceStore


async def scenario(
    store: FinanceStore, tmp_path: Path
) -> tuple[FinancePrincipal, EncryptedLocalObjects, PdfGateway, bytes, dict[str, Any]]:
    """先完成F7真实原件基线，再建立待处理材料，不伪造回执或决定。"""
    owner, _ = await configured(store)
    objects = EncryptedLocalObjects(tmp_path / "objects", b"k" * 32)
    _, raw = await source(store, owner, objects, "baseline")
    operation = await store.accept_supporting_materials(owner, "e", [ref("target", raw)], "target")
    return owner, objects, PdfGateway({"target": raw, "following": raw}), raw, operation


async def revoke(store: FinanceStore, owner: FinancePrincipal) -> None:
    """真实删除本租户公司的申请人授权，并等待撤权事务提交。"""
    result = await store.pool.execute(
        "DELETE FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id='e' AND user_id=$2",
        owner.tenant_id,
        owner.user_id,
    )
    assert result == "DELETE 1"


async def original_args(store: FinanceStore, claim: dict[str, Any], raw: bytes) -> tuple[Any, ...]:
    """通过真实写前索引取得回执调用参数，不直接写任何收件状态。"""
    item = await store.prepare_original(
        claim, 0, ref("target", raw).sha256, len(raw), "application/pdf", "target.pdf"
    )
    return (
        claim,
        0,
        item["object_key"],
        item["actual_sha256"],
        item["size_bytes"],
        item["content_type"],
        item["filename"],
    )


async def assert_failed(
    store: FinanceStore,
    operation: dict[str, Any],
    objects: EncryptedLocalObjects,
    raw: bytes,
    window: str,
) -> None:
    """核对撤权失败责任、原件保留边界和真实事件，禁止成功材料事件。"""
    item = await store.pool.fetchrow(
        "SELECT * FROM finance_item WHERE operation_id=$1", operation["id"]
    )
    assert item is not None
    assert item["state"] == "failed" and item["error_code"] == "COMPANY_FORBIDDEN"
    failure = await store.pool.fetchrow(
        "SELECT * FROM finance_exception WHERE id=$1", item["exception_id"]
    )
    assert failure is not None and failure["rule_id"] == "COMPANY_FORBIDDEN"
    assert failure["owner_id"] == await store.pool.fetchval(
        "SELECT owner_id FROM finance_operation WHERE id=$1", operation["id"]
    )
    assert failure["receipt_id"] == item["receipt_id"]
    events = await store.pool.fetch(
        "SELECT kind,data FROM finance_event WHERE aggregate_id=$1", operation["id"]
    )
    kinds = [event["kind"] for event in events]
    assert "material.received" not in kinds
    assert kinds.count("import.item_exception") == 1
    event = next(event for event in events if event["kind"] == "import.item_exception")
    assert decode(event["data"])["code"] == "COMPANY_FORBIDDEN"
    receipts = await store.pool.fetchval(
        "SELECT count(*) FROM finance_receipt WHERE operation_id=$1", operation["id"]
    )
    assert receipts == int(window == "received")
    assert kinds.count("original.prepared") == int(window != "downloaded")
    assert kinds.count("original.received") == int(window == "received")
    if window == "downloaded":
        assert item["object_key"] is None and item["actual_sha256"] is None
        assert len(list(objects.root.rglob("*.enc"))) == 1
    else:
        assert item["object_key"] and await objects.get(item["object_key"]) == raw
        assert len(list(objects.root.rglob("*.enc"))) == 2
    if window == "received":
        receipt = await store.pool.fetchrow(
            "SELECT * FROM finance_receipt WHERE id=$1", item["receipt_id"]
        )
        assert receipt is not None and receipt["object_key"] == item["object_key"]
    assert await store.pool.fetchval("SELECT count(*) FROM finance_invoice") == 0


async def worker_facts(
    request: pytest.FixtureRequest,
    store: FinanceStore,
    operation: dict[str, Any],
    following: dict[str, Any],
    objects: EncryptedLocalObjects,
    window: str,
) -> None:
    """将实际DB状态、事件与密文数写入JUnit属性，不含连接信息或原件正文。"""
    item = await store.pool.fetchrow(
        "SELECT state,error_code,object_key FROM finance_item WHERE operation_id=$1",
        operation["id"],
    )
    assert item is not None
    events = await store.pool.fetch(
        "SELECT kind FROM finance_event WHERE aggregate_id=$1 ORDER BY sequence", operation["id"]
    )
    cipher = bool(item["object_key"] and objects._path(item["object_key"]).is_file())
    facts = {
        "f12_window": window,
        "f12_operation_id": operation["id"],
        "f12_item_state": item["state"],
        "f12_error_code": item["error_code"],
        "f12_receipt_count": await store.pool.fetchval(
            "SELECT count(*) FROM finance_receipt WHERE operation_id=$1", operation["id"]
        ),
        "f12_target_cipher_count": int(cipher),
        "f12_next_operation_status": await store.pool.fetchval(
            "SELECT status FROM finance_operation WHERE id=$1", following["id"]
        ),
        "f12_next_item_state": await store.pool.fetchval(
            "SELECT state FROM finance_item WHERE operation_id=$1", following["id"]
        ),
        "f12_db_event_kinds": json.dumps([event["kind"] for event in events]),
        "f12_original_source": "F7 configured/source + actual PDF + real FinanceStore/local objects",
    }
    request.node.user_properties.extend(facts.items())
    request.node.user_properties.append(("f12_worker", json.dumps(facts, ensure_ascii=False)))

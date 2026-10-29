"""F12实际PG撤权窗口、逐项责任和worker持续处理的回归。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg
import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.models import FileRef, FinancePrincipal

from .f12_worker_helpers import assert_failed, original_args, revoke, scenario, worker_facts
from .test_worker import ref

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


@pytest.mark.parametrize("window", ["downloaded", "written", "received"])
async def test_revocation_at_original_checkpoint_preserves_responsibility_and_worker(
    store: FinanceStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    window: str,
    request: pytest.FixtureRequest,
) -> None:
    """下载后、密文写后、回执提交后撤权只失败当前项，同实例继续合法申请人。"""
    owner, objects, gateway, raw, operation = await scenario(store, tmp_path)
    other = FinancePrincipal(owner.tenant_id, "following", origin="human")
    await store.grant(other.tenant_id, other.user_id, "e")
    following = await store.accept_supporting_materials(
        other, "e", [ref("following", raw)], "following"
    )
    download, put, receive = gateway.download, objects.put, store.receive_original

    async def after_download(who: FinancePrincipal, file: FileRef) -> tuple[bytes, str, str]:
        """实际网关返回源字节后等待授权删除提交。"""
        result = await download(who, file)
        if file.file_id == "target":
            await revoke(store, owner)
        return result

    async def after_write(key: str, data: bytes) -> None:
        """实际本地密文写成功后等待授权删除提交。"""
        await put(key, data)
        if operation["id"] in key:
            assert await objects.get(key) == raw
            await revoke(store, owner)

    async def after_receive(*args: Any) -> dict[str, Any]:
        """真实回执事务提交后撤权，不伪造收件或完成结果。"""
        result = await receive(*args)
        if args[0]["id"] == operation["id"]:
            assert (
                await store.pool.fetchval(
                    "SELECT count(*) FROM finance_receipt WHERE operation_id=$1", operation["id"]
                )
                == 1
            )
            await revoke(store, owner)
        return result

    target, attribute, wrapper = {
        "downloaded": (gateway, "download", after_download),
        "written": (objects, "put", after_write),
        "received": (store, "receive_original", after_receive),
    }[window]
    monkeypatch.setattr(target, attribute, wrapper)
    worker = FinanceWorker(store, objects, gateway)
    # 捕获仅为提供明确断言；产品必须自行记录预期拒绝并正常返回。
    failure = None
    try:
        assert await worker.run_once()
    except ExceptionGroup as exc:
        failure = exc
    assert failure is None, f"预期公司撤权不应终止worker：{failure!r}"
    await assert_failed(store, operation, objects, raw, window)
    assert await worker.run_once()
    detail = await store.get_supporting_material_import(other, following["id"])
    assert detail["operation"]["status"] == "succeeded"
    assert detail["materials"][0]["state"] == "material_received"
    assert detail["materials"][0]["selectable"] is True
    assert not await worker.run_once()
    await worker_facts(request, store, operation, following, objects, window)


@pytest.mark.parametrize("checkpoint", ["prepare", "receive"])
@pytest.mark.parametrize("expired", [False, True])
async def test_direct_original_checkpoint_checks_current_authority_and_lease(
    store: FinanceStore, tmp_path: Path, checkpoint: str, expired: bool
) -> None:
    """直接存储调用也拒绝当前撤权；租约先失效时仍返回fencing且无新增回执。"""
    owner, objects, _, raw, operation = await scenario(store, tmp_path)
    claim = await store.claim_import()
    assert claim is not None and claim["id"] == operation["id"]
    if checkpoint == "receive":
        args = await original_args(store, claim, raw)
        await objects.put(args[2], raw)
    else:
        args = (claim, 0, ref("target", raw).sha256, len(raw), "application/pdf", "target.pdf")
    await revoke(store, owner)
    if expired:
        await store.pool.execute(
            "UPDATE finance_operation SET lease_until=now()-interval '1 second' WHERE id=$1",
            operation["id"],
        )
    method = store.prepare_original if checkpoint == "prepare" else store.receive_original
    with pytest.raises(FinanceError, match="LEASE_LOST" if expired else "COMPANY_FORBIDDEN"):
        await method(*args)
    item = await store.pool.fetchrow(
        "SELECT * FROM finance_item WHERE operation_id=$1", operation["id"]
    )
    assert item is not None and item["state"] == "pending"
    assert (item["object_key"] is not None) == (checkpoint == "receive")
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_receipt WHERE operation_id=$1", operation["id"]
        )
        == 0
    )
    if expired:
        with pytest.raises(FinanceError, match="LEASE_LOST"):
            await store.finish_item_error(claim, 0, "failed", "COMPANY_FORBIDDEN")
        assert item["exception_id"] is None


@pytest.mark.parametrize("kind", ["original.prepared", "original.received", "material.received"])
async def test_checkpoint_database_fault_propagates_and_recovers_without_duplicate_receipt(
    store: FinanceStore, tmp_path: Path, kind: str
) -> None:
    """真实PG事件故障向上传播并回滚本检查点，恢复后原请求只保留一个回执。"""
    _, objects, gateway, _, operation = await scenario(store, tmp_path)
    await store.pool.execute(
        "CREATE FUNCTION fail_f12_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.kind = '"
        + kind
        + "' THEN RAISE EXCEPTION 'F12 checkpoint database failure'; END IF; RETURN NEW; END $$; "
        "CREATE TRIGGER f12_event_failure BEFORE INSERT ON finance_event FOR EACH ROW EXECUTE FUNCTION fail_f12_event()"
    )
    worker = FinanceWorker(store, objects, gateway)
    with pytest.raises(ExceptionGroup) as raised:
        await worker.run_once()
    assert any(isinstance(error, asyncpg.PostgresError) for error in raised.value.exceptions)
    item = await store.pool.fetchrow(
        "SELECT * FROM finance_item WHERE operation_id=$1", operation["id"]
    )
    assert item is not None
    assert item["state"] == ("received" if kind == "material.received" else "pending")
    assert item["error_code"] is None and item["exception_id"] is None
    assert await store.pool.fetchval(
        "SELECT count(*) FROM finance_receipt WHERE operation_id=$1", operation["id"]
    ) == int(kind == "material.received")
    await store.pool.execute("DROP TRIGGER f12_event_failure ON finance_event")
    assert await worker.run_once()
    assert gateway.downloads == (2 if kind == "original.prepared" else 1)
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_receipt WHERE operation_id=$1", operation["id"]
        )
        == 1
    )
    assert (
        await store.pool.fetchval(
            "SELECT state FROM finance_item WHERE operation_id=$1", operation["id"]
        )
        == "material_received"
    )


@pytest.mark.parametrize(
    "method", ["prepare_original", "receive_original", "finish_supporting_item"]
)
@pytest.mark.parametrize("fault", ["domain", "io"])
async def test_unrelated_checkpoint_errors_are_not_converted_to_company_failure(
    store: FinanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, fault: str
) -> None:
    """新增撤权处理不得吞掉其他领域错误或检查点IO故障。"""
    _, objects, gateway, _, operation = await scenario(store, tmp_path)
    error = (
        FinanceError("UNEXPECTED_CHECKPOINT", 409, "测试未预期检查点")
        if fault == "domain"
        else OSError("F12 checkpoint unavailable")
    )

    async def fail(*args: Any, **kwargs: Any) -> Any:
        """仅注入指定检查点异常，其他PG与原件过程保持真实。"""
        raise error

    monkeypatch.setattr(store, method, fail)
    with pytest.raises(ExceptionGroup) as raised:
        await FinanceWorker(store, objects, gateway).run_once()
    assert error in raised.value.exceptions
    item = await store.pool.fetchrow(
        "SELECT * FROM finance_item WHERE operation_id=$1", operation["id"]
    )
    assert item is not None and item["error_code"] is None and item["exception_id"] is None


@pytest.mark.parametrize(
    "checkpoint", ["prepare_original", "receive_original", "finish_supporting_item"]
)
async def test_revocation_error_recording_cannot_bypass_expired_lease(
    store: FinanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint: str
) -> None:
    """撤权已识别但失败责任落库前失租约，旧worker退让且不能强写异常。"""
    owner, objects, gateway, _, operation = await scenario(store, tmp_path)
    original_checkpoint = getattr(store, checkpoint)
    finish_error = store.finish_item_error
    attempted = False

    async def revoke_before_checkpoint(*args: Any) -> Any:
        """在真实检查点之前删除授权，再调用真实事务复核。"""
        await revoke(store, owner)
        return await original_checkpoint(*args)

    async def expire_before_error(*args: Any) -> None:
        """失败处理真实执行前令租约过期，依旧经过存储fencing。"""
        nonlocal attempted
        attempted = True
        await store.pool.execute(
            "UPDATE finance_operation SET lease_until=now()-interval '1 second' WHERE id=$1",
            operation["id"],
        )
        await finish_error(*args)

    monkeypatch.setattr(store, checkpoint, revoke_before_checkpoint)
    monkeypatch.setattr(store, "finish_item_error", expire_before_error)
    assert await FinanceWorker(store, objects, gateway).run_once()
    assert attempted
    item = await store.pool.fetchrow(
        "SELECT * FROM finance_item WHERE operation_id=$1", operation["id"]
    )
    assert item is not None
    assert item["state"] == ("received" if checkpoint == "finish_supporting_item" else "pending")
    assert item["error_code"] is None and item["exception_id"] is None
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_exception WHERE operation_id=$1", operation["id"]
        )
        == 0
    )

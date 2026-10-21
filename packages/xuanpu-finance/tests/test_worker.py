"""验证原件加密、逐项持久化与异常补件恢复。"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.invoices.worker import FinanceWorker
from xuanpu_finance.models import FileRef, FinancePrincipal
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.storage import FinanceStore

from .test_parser import SAMPLE
from .test_storage import WHO, prepare

if TYPE_CHECKING:
    from pathlib import Path


class Gateway:
    """显式内存平台文件边界，保留下载次数。"""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        """注入不可变测试原件。"""
        self.payloads = payloads
        self.downloads = 0
        self.enabled = True

    async def active(self, who: FinancePrincipal) -> bool:
        """模拟平台当前账号状态。"""
        return self.enabled

    async def download(self, who: FinancePrincipal, file: FileRef) -> tuple[bytes, str, str]:
        """返回原字节及显式元数据。"""
        self.downloads += 1
        return self.payloads[file.file_id], "application/xml", file.file_id + ".xml"


def ref(name: str, data: bytes) -> FileRef:
    """构造绑定原件摘要的引用。"""
    return FileRef(file_id=name, revision=1, sha256=hashlib.sha256(data).hexdigest())


async def test_original_is_encrypted_and_authenticated(tmp_path: Path) -> None:
    """磁盘不含明文且密文篡改必须失败。"""
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    await objects.put("tenant/company/file", SAMPLE)
    assert await objects.get("tenant/company/file") == SAMPLE
    files = list(tmp_path.rglob("*.enc"))
    assert len(files) == 1 and SAMPLE not in files[0].read_bytes()
    files[0].write_bytes(files[0].read_bytes()[:-1] + b"X")
    with pytest.raises(FinanceError, match="OBJECT_CORRUPT"):
        await objects.get("tenant/company/file")
    with pytest.raises(FinanceError, match="OBJECT_KEY_INVALID"):
        await objects.put("../../escape", b"x")


async def test_worker_duplicate_and_field_evidence_pagination(
    store: FinanceStore, tmp_path: Path
) -> None:
    """重启后不重建已提交票且全量分页计数保持稳定。"""
    await prepare(store)
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    gateway = Gateway({"a": SAMPLE, "b": SAMPLE})
    op = await store.accept_import(WHO, "company-a", [ref("a", SAMPLE), ref("b", SAMPLE)], "import")
    assert await FinanceWorker(store, objects, gateway, synthetic=True).run_once()
    result = await store.get_operation(WHO, op["id"])
    assert result["status"] == "succeeded" and result["summary"]["parsed"] == 1
    assert result["summary"]["duplicate_linked"] == 1
    assert not await FinanceWorker(
        FinanceStore(store.pool), objects, gateway, synthetic=True
    ).run_once()
    invoices = await store.list_invoices(WHO, "company-a")
    assert invoices["total"] == 1
    invoice_id = invoices["items"][0]["id"]
    fields = await store.list_invoice_fields(WHO, invoice_id, limit=1)
    following = await store.list_invoice_fields(WHO, invoice_id, fields["next_cursor"], 1)
    assert fields["total"] > 1 and following["total"] == fields["total"]
    assert fields["items"][0]["source_receipt_id"]
    assert (await store.list_invoice_lines(WHO, invoice_id))["total"] == 1
    assert len((await store.get_invoice_evidence(WHO, invoice_id))["evidence"]) == 2


async def test_unknown_original_and_hash_mismatch_are_durable(
    store: FinanceStore, tmp_path: Path
) -> None:
    """未知格式保留字节并待补原件，哈希不符单独隔离。"""
    await prepare(store)
    pdf = b"%PDF-1.4 opaque original"
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    gateway = Gateway({"pdf": pdf, "bad": SAMPLE})
    op = await store.accept_import(
        WHO,
        "company-a",
        [ref("pdf", pdf), FileRef(file_id="bad", revision=1, sha256="0" * 64)],
        "unknown",
    )
    await FinanceWorker(store, objects, gateway).run_once()
    result = await store.get_operation(WHO, op["id"])
    assert result["summary"]["awaiting_original"] == 1 and result["summary"]["quarantined"] == 1
    assert (await store.list_exceptions(WHO, "company-a"))["total"] == 2
    record = await store.pool.fetchrow(
        "SELECT object_key FROM finance_item WHERE operation_id=$1 AND position=0", op["id"]
    )
    assert record is not None and await objects.get(record["object_key"]) == pdf


async def test_supplement_closes_only_after_new_invoice_commit(
    store: FinanceStore, tmp_path: Path
) -> None:
    """重建 worker 和存储后补原件，失败不关闭旧材料缺失异常。"""
    await prepare(store)
    pdf = b"%PDF-1.4"
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    gateway = Gateway({"pdf": pdf, "xml": SAMPLE})
    first = await store.accept_import(WHO, "company-a", [ref("pdf", pdf)], "missing")
    await FinanceWorker(store, objects, gateway).run_once()
    source = (await store.get_operation(WHO, first["id"]))["items"][0]["exception_id"]
    failed = await store.accept_import(
        WHO,
        "company-a",
        [ref("pdf", pdf)],
        "retry-bad",
        source_exception_id=source,
        expected_exception_version=1,
    )
    await FinanceWorker(store, objects, gateway).run_once()
    assert (await store.get_operation(WHO, failed["id"]))["summary"]["awaiting_original"] == 1
    assert (
        next(
            item
            for item in (await store.list_exceptions(WHO, "company-a"))["items"]
            if item["id"] == source
        )["status"]
        == "open"
    )
    restored = FinanceStore(store.pool)
    repaired = await restored.accept_import(
        WHO,
        "company-a",
        [ref("xml", SAMPLE)],
        "retry-good",
        source_exception_id=source,
        expected_exception_version=1,
    )
    await FinanceWorker(restored, objects, gateway, synthetic=True).run_once()
    assert (await restored.get_operation(WHO, repaired["id"]))["summary"]["parsed"] == 1
    original = await store.pool.fetchrow("SELECT * FROM finance_exception WHERE id=$1", source)
    assert (
        original is not None
        and original["status"] == "resolved"
        and original["resolved_invoice_id"]
    )
    assert (
        len((await store.get_invoice_evidence(WHO, original["resolved_invoice_id"]))["evidence"])
        == 2
    )


class BrokenObjects:
    """显式模拟原件存储失败。"""

    async def put(self, key: str, content: bytes) -> None:
        """写入失败而不冒充回执已保留。"""
        raise OSError("object storage unavailable")

    async def get(self, key: str) -> bytes:
        """读取失败。"""
        raise OSError("object storage unavailable")


async def test_storage_fault_records_responsibility(store: FinanceStore) -> None:
    """对象存储故障必须成为失败项和持久异常。"""
    await prepare(store)
    op = await store.accept_import(WHO, "company-a", [ref("x", SAMPLE)], "fault")
    await FinanceWorker(store, BrokenObjects(), Gateway({"x": SAMPLE}), synthetic=True).run_once()
    result = await store.get_operation(WHO, op["id"])
    assert result["items"][0]["error_code"] == "OBJECT_STORAGE_FAILED"
    assert result["summary"]["failed"] == 1 and result["items"][0]["exception_id"]
    assert result["status"] == "failed" and result["business_result"] == "failed"


async def test_local_original_is_immutable_across_adapters(tmp_path: Path) -> None:
    """同 key 相同内容重放，不同内容绝不覆盖原字节。"""
    first = EncryptedLocalObjects(tmp_path, b"k" * 32)
    other = EncryptedLocalObjects(tmp_path, b"k" * 32)
    await first.put("t/c/original", SAMPLE)
    await other.put("t/c/original", SAMPLE)
    with pytest.raises(FinanceError, match="OBJECT_IMMUTABLE"):
        await other.put("t/c/original", b"different")
    assert await first.get("t/c/original") == SAMPLE


async def test_s3_original_encrypted_and_immutable() -> None:
    """真实 S3 客户端经 moto 验证原子条件创建与密文保全。"""
    import boto3
    from moto import mock_aws
    from xuanpu_finance.objects import EncryptedS3Objects

    with mock_aws():
        client = boto3.client(
            "s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
        )
        client.create_bucket(Bucket="finance-test")
        objects = EncryptedS3Objects(client, "finance-test", b"k" * 32)
        await objects.put("t/c/original", SAMPLE)
        await objects.put("t/c/original", SAMPLE)
        with pytest.raises(FinanceError, match="OBJECT_IMMUTABLE"):
            await objects.put("t/c/original", b"different")
        assert await objects.get("t/c/original") == SAMPLE
        entries = client.list_objects_v2(Bucket="finance-test")["Contents"]
        encrypted = client.get_object(Bucket="finance-test", Key=entries[0]["Key"])["Body"].read()
        assert SAMPLE not in encrypted


async def test_database_failure_rolls_back_invoice_and_restarts_from_received(
    store: FinanceStore, tmp_path: Path
) -> None:
    """真实数据库事件写入故障回滚票、明细与闭环，恢复后不重复下载。"""
    await prepare(store)
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    gateway = Gateway({"x": SAMPLE})
    operation = await store.accept_import(WHO, "company-a", [ref("x", SAMPLE)], "db-fault")
    await store.pool.execute(
        "CREATE FUNCTION fail_invoice_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.kind = 'invoice.parsed' THEN RAISE EXCEPTION 'test event DB failure'; END IF; RETURN NEW; END $$; CREATE TRIGGER injected_event_failure BEFORE INSERT ON finance_event FOR EACH ROW EXECUTE FUNCTION fail_invoice_event()"
    )
    with pytest.raises(ExceptionGroup):
        await FinanceWorker(store, objects, gateway, synthetic=True).run_once()
    assert await store.pool.fetchval("SELECT count(*) FROM finance_invoice") == 0
    assert (await store.get_operation(WHO, operation["id"]))["items"][0]["state"] == "received"
    await store.pool.execute("DROP TRIGGER injected_event_failure ON finance_event")
    await FinanceWorker(FinanceStore(store.pool), objects, gateway, synthetic=True).run_once()
    assert (await store.get_operation(WHO, operation["id"]))["summary"]["parsed"] == 1
    assert gateway.downloads == 1


async def test_concurrent_workers_only_create_one_main_invoice(
    store: FinanceStore, tmp_path: Path
) -> None:
    """多个持久批次与 worker 同时竞争主票唯一约束。"""
    import asyncio

    await prepare(store)
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    gateway = Gateway({"x": SAMPLE})
    operations = await asyncio.gather(
        *(
            store.accept_import(WHO, "company-a", [ref("x", SAMPLE)], f"parallel-{index}")
            for index in range(8)
        )
    )
    assert all(
        await asyncio.gather(
            *(FinanceWorker(store, objects, gateway, synthetic=True).run_once() for _ in range(8))
        )
    )
    assert await store.pool.fetchval("SELECT count(*) FROM finance_invoice") == 1
    results = await asyncio.gather(*(store.get_operation(WHO, op["id"]) for op in operations))
    assert sum(op["summary"]["parsed"] for op in results) == 1
    assert sum(op["summary"]["duplicate_linked"] for op in results) == 7


async def test_stale_worker_fenced_after_lease_recovery(
    store: FinanceStore, tmp_path: Path
) -> None:
    """模拟进程硬停租约到期，重启恢复且旧 worker 无权提交。"""
    await prepare(store)
    operation = await store.accept_import(WHO, "company-a", [ref("x", SAMPLE)], "stale")
    stale = await store.claim_import()
    assert stale is not None
    await store.pool.execute(
        "UPDATE finance_operation SET lease_until=now()-interval '1 second' WHERE id=$1",
        operation["id"],
    )
    await FinanceWorker(
        FinanceStore(store.pool),
        EncryptedLocalObjects(tmp_path, b"k" * 32),
        Gateway({"x": SAMPLE}),
        synthetic=True,
    ).run_once()
    with pytest.raises(FinanceError, match="LEASE_LOST"):
        await store.finish_item_error(stale, 0, "failed", "OLD_WORKER")
    assert (await store.get_operation(WHO, operation["id"]))["summary"]["parsed"] == 1


async def test_size_limits_apply_to_original_and_aggregate(
    store: FinanceStore, tmp_path: Path
) -> None:
    """实际 20/100 MiB 边界不以测试配置放宽，超限不生成回执。"""
    await prepare(store)
    payload = b"%PDF" + b"x" * (20 * 1024 * 1024 - 4)
    gateway = Gateway({str(index): payload for index in range(6)} | {"oversize": payload + b"x"})
    operation = await store.accept_import(
        WHO,
        "company-a",
        [ref(str(index), payload) for index in range(6)] + [ref("oversize", payload + b"x")],
        "limits",
    )
    await FinanceWorker(store, EncryptedLocalObjects(tmp_path, b"k" * 32), gateway).run_once()
    result = await store.get_operation(WHO, operation["id"])
    assert result["summary"]["awaiting_original"] == 5 and result["summary"]["quarantined"] == 2
    assert [item.get("error_code") for item in result["items"][-2:]] == [
        "IMPORT_SIZE_EXCEEDED",
        "FILE_TOO_LARGE",
    ]
    assert (
        await store.pool.fetchval(
            "SELECT sum(size_bytes) FROM finance_item WHERE operation_id=$1", operation["id"]
        )
        == 100 * 1024 * 1024
    )
    assert len(list(tmp_path.rglob("*.enc"))) == 5


async def test_cancel_during_download_preserves_finished_invoice(
    store: FinanceStore, tmp_path: Path
) -> None:
    """取消正在下载的后续项，已提交票保留且旧 worker 正常退出。"""
    import asyncio

    class PausingGateway(Gateway):
        """仅第二项在可观察屏障等待。"""

        def __init__(self) -> None:
            """建立异步测试屏障。"""
            super().__init__({"first": SAMPLE, "second": SAMPLE})
            self.ready = asyncio.Event()
            self.resume = asyncio.Event()

        async def download(self, who: FinancePrincipal, file: FileRef) -> tuple[bytes, str, str]:
            """第二项下载期间允许调用用户取消接口。"""
            if file.file_id == "second":
                self.ready.set()
                await self.resume.wait()
            return await super().download(who, file)

    await prepare(store)
    gateway = PausingGateway()
    operation = await store.accept_import(
        WHO, "company-a", [ref("first", SAMPLE), ref("second", SAMPLE)], "cancel-midflight"
    )
    task = asyncio.create_task(
        FinanceWorker(
            store, EncryptedLocalObjects(tmp_path, b"k" * 32), gateway, synthetic=True
        ).run_once()
    )
    await asyncio.wait_for(gateway.ready.wait(), 10)
    current = await store.get_operation(WHO, operation["id"])
    await store.cancel_operation(WHO, operation["id"], current["version"], "cancel-midflight-key")
    gateway.resume.set()
    assert await task
    current = await store.get_operation(WHO, operation["id"])
    assert (
        current["status"] == "cancelled"
        and current["summary"]["parsed"] == 1
        and current["summary"]["cancelled"] == 1
    )
    assert await store.pool.fetchval("SELECT count(*) FROM finance_invoice") == 1
    assert len(list(tmp_path.rglob("*.enc"))) == 1


async def test_invoice_children_evidence_and_duplicates_keep_tenant_company_scope(
    store: FinanceStore, tmp_path: Path
) -> None:
    """跨租户同公司名同票号独立，子表证据严格继承公司及本人权限。"""
    await prepare(store)
    other = FinancePrincipal("tenant-b", "user-a")
    await store.seed_company(other.tenant_id, "company-a", "B公司", "TAX-B")
    await store.grant(other.tenant_id, other.user_id, "company-a")
    await store.seed_company(WHO.tenant_id, "company-b", "C公司", "TAX-C")
    await store.grant(WHO.tenant_id, WHO.user_id, "company-b")
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    gateway = Gateway({"x": SAMPLE})
    for who, company in ((WHO, "company-a"), (other, "company-a"), (WHO, "company-b")):
        await store.accept_import(
            who,
            company,
            [ref("x", SAMPLE)],
            "same-key" if company == "company-a" else "other-company",
        )
        await FinanceWorker(store, objects, gateway, synthetic=True).run_once()
        assert (await store.list_invoices(who, company))["total"] == 1
    assert await store.pool.fetchval("SELECT count(*) FROM finance_invoice") == 3
    invoice_id = (await store.list_invoices(WHO, "company-a"))["items"][0]["id"]
    await store.grant(WHO.tenant_id, "other-user", "company-a")
    outsider = FinancePrincipal(WHO.tenant_id, "other-user")
    assert (await store.list_invoices(outsider, "company-a"))["total"] == 0
    for reader in (
        store.get_invoice,
        store.list_invoice_lines,
        store.list_invoice_fields,
        store.get_invoice_evidence,
    ):
        for who in (other, outsider):
            with pytest.raises(FinanceError):
                await reader(who, invoice_id)
    await store.grant(WHO.tenant_id, "reviewer", "company-a", "reviewer")
    assert (await store.get_invoice(FinancePrincipal(WHO.tenant_id, "reviewer"), invoice_id))[
        "id"
    ] == invoice_id


@pytest.mark.parametrize("kind", ["invoices", "exceptions"])
async def test_business_keyset_pages_keep_new_insertions(
    store: FinanceStore, tmp_path: Path, kind: str
) -> None:
    """发票及异常翻页期间新增记录，旧记录不丢失且没有重复。"""
    await prepare(store)
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    created: list[str] = []

    async def insert(name: str) -> None:
        """通过实际受理和 worker 产生领域记录。"""
        data = (
            SAMPLE.replace(b"SYN-001", name.encode())
            if kind == "invoices"
            else b"%PDF" + name.encode()
        )
        operation = await store.accept_import(WHO, "company-a", [ref(name, data)], "keyset-" + name)
        await FinanceWorker(store, objects, Gateway({name: data}), synthetic=True).run_once()
        result = await store.get_operation(WHO, operation["id"])
        created.append(result["items"][0]["invoice_id" if kind == "invoices" else "exception_id"])

    for name in ("b", "d", "f"):
        await insert(name)
    listing = store.list_invoices if kind == "invoices" else store.list_exceptions
    first = await listing(WHO, "company-a", limit=1)
    assert first["items"][0]["id"] == created[0]
    await insert("a")
    ids = [first["items"][0]["id"]]
    cursor = first["next_cursor"]
    while cursor is not None:
        following = await listing(WHO, "company-a", cursor, 1)
        assert following["total"] == 4
        ids.extend(item["id"] for item in following["items"])
        cursor = following["next_cursor"]
    assert ids == created and len(set(ids)) == 4


async def test_unknown_xml_profile_is_quarantined_with_original_retained(
    store: FinanceStore, tmp_path: Path
) -> None:
    """真实未知 XML 不能冒充材料缺失；隔离但原字节及异常均持久保留。"""
    await prepare(store)
    data = b'<Invoice xmlns="urn:unconfigured:official-looking"><Amount>100.00</Amount></Invoice>'
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    operation = await store.accept_import(WHO, "company-a", [ref("unknown", data)], "unknown-xml")
    await FinanceWorker(store, objects, Gateway({"unknown": data})).run_once()
    result = await store.get_operation(WHO, operation["id"])
    assert result["summary"]["quarantined"] == 1 and result["summary"]["awaiting_original"] == 0
    assert (
        result["items"][0]["exception_id"]
        and result["items"][0]["error_code"] == "PARSER_PROFILE_UNCONFIGURED"
    )
    item = await store.pool.fetchrow(
        "SELECT * FROM finance_item WHERE operation_id=$1", operation["id"]
    )
    assert item is not None and item["receipt_id"] and await objects.get(item["object_key"]) == data


async def test_original_receipt_database_failure_recovers_without_platform_source(
    store: FinanceStore, tmp_path: Path
) -> None:
    """原件已落盘但回执事务失败，删除平台原件后仍从责任索引完成恢复。"""
    await prepare(store)
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    gateway = Gateway({"original": SAMPLE})
    operation = await store.accept_import(
        WHO, "company-a", [ref("original", SAMPLE)], "receipt-db-fault"
    )
    await store.pool.execute(
        "CREATE FUNCTION fail_original_receipt() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.kind = 'original.received' THEN RAISE EXCEPTION 'test receipt DB failure'; END IF; RETURN NEW; END $$; CREATE TRIGGER injected_receipt_failure BEFORE INSERT ON finance_event FOR EACH ROW EXECUTE FUNCTION fail_original_receipt()"
    )
    with pytest.raises(ExceptionGroup):
        await FinanceWorker(store, objects, gateway, synthetic=True).run_once()
    assert len(list(tmp_path.rglob("*.enc"))) == 1
    assert await store.pool.fetchval("SELECT count(*) FROM finance_receipt") == 0
    item = await store.pool.fetchrow(
        "SELECT * FROM finance_item WHERE operation_id=$1", operation["id"]
    )
    assert item is not None and item["state"] == "pending" and item["object_key"]
    assert item["actual_sha256"] == hashlib.sha256(SAMPLE).hexdigest() and item[
        "size_bytes"
    ] == len(SAMPLE)
    gateway.payloads.clear()
    await store.pool.execute("DROP TRIGGER injected_receipt_failure ON finance_event")
    await FinanceWorker(
        FinanceStore(store.pool),
        EncryptedLocalObjects(tmp_path, b"k" * 32),
        gateway,
        synthetic=True,
    ).run_once()
    result = await store.get_operation(WHO, operation["id"])
    assert result["status"] == "succeeded" and result["summary"]["parsed"] == 1
    assert (
        gateway.downloads == 1
        and await store.pool.fetchval("SELECT count(*) FROM finance_receipt") == 1
    )


@pytest.mark.parametrize(
    "payload,content_type,filename",
    [
        (b"%PDF-1.4 opaque", "application/pdf", "source.pdf"),
        (b"\x89PNG\r\n\x1a\nopaque", "image/png", "source.png"),
        (b"PK\x03\x04opaque", "application/ofd", "source.ofd"),
    ],
)
async def test_binary_originals_wait_for_parseable_original(
    store: FinanceStore, tmp_path: Path, payload: bytes, content_type: str, filename: str
) -> None:
    """PDF、图片及 OFD 仅保全不执行，不误判为未知 XML profile。"""

    class TypedGateway(Gateway):
        """提供不同原件封装的明确元数据。"""

        async def download(self, who: FinancePrincipal, file: FileRef) -> tuple[bytes, str, str]:
            """返回实际原字节和对应封装元数据。"""
            data, _, _ = await super().download(who, file)
            return data, content_type, filename

    await prepare(store)
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    operation = await store.accept_import(WHO, "company-a", [ref("binary", payload)], "binary")
    await FinanceWorker(store, objects, TypedGateway({"binary": payload})).run_once()
    result = await store.get_operation(WHO, operation["id"])
    assert result["summary"]["awaiting_original"] == 1 and result["summary"]["quarantined"] == 0
    item = await store.pool.fetchrow(
        "SELECT object_key FROM finance_item WHERE operation_id=$1", operation["id"]
    )
    assert item is not None and await objects.get(item["object_key"]) == payload


async def test_prepared_missing_object_cannot_rebind_changed_platform_source(
    store: FinanceStore, tmp_path: Path
) -> None:
    """预登记后写入前停机，平台源变化必须隔离，不能改绑已有责任摘要。"""
    import asyncio

    class InterruptedObjects(EncryptedLocalObjects):
        """模拟对象写入前进程被中断。"""

        async def put(self, key: str, content: bytes) -> None:
            """留下已提交责任索引但不写任何对象。"""
            raise asyncio.CancelledError

    await prepare(store)
    gateway = Gateway({"source": SAMPLE})
    operation = await store.accept_import(
        WHO, "company-a", [ref("source", SAMPLE)], "prepared-rebind"
    )
    with pytest.raises(asyncio.CancelledError):
        await FinanceWorker(
            store, InterruptedObjects(tmp_path, b"k" * 32), gateway, synthetic=True
        ).run_once()
    assert not list(tmp_path.rglob("*.enc"))
    gateway.payloads["source"] = SAMPLE.replace(b"SYN-001", b"SYN-CHANGED")
    await FinanceWorker(
        FinanceStore(store.pool),
        EncryptedLocalObjects(tmp_path, b"k" * 32),
        gateway,
        synthetic=True,
    ).run_once()
    result = await store.get_operation(WHO, operation["id"])
    assert (
        result["summary"]["quarantined"] == 1
        and result["items"][0]["error_code"] == "ORIGINAL_INDEX_CONFLICT"
    )
    assert not list(tmp_path.rglob("*.enc"))


async def test_parser_runs_in_importable_cancellable_process(
    store: FinanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """实际子进程执行合成解析，不能退回共享线程。"""
    from anyio import to_process

    actual = to_process.run_sync
    calls: list[bool] = []

    async def observed(function: object, *args: object, **kwargs: object) -> object:
        """保留真正进程执行并观察取消开关。"""
        calls.append(kwargs.get("cancellable") is True)
        return await actual(function, *args, **kwargs)

    monkeypatch.setattr(to_process, "run_sync", observed)
    await prepare(store)
    operation = await store.accept_import(
        WHO, "company-a", [ref("process", SAMPLE)], "parse-process"
    )
    await FinanceWorker(
        store,
        EncryptedLocalObjects(tmp_path, b"k" * 32),
        Gateway({"process": SAMPLE}),
        synthetic=True,
    ).run_once()
    assert (
        calls == [True]
        and (await store.get_operation(WHO, operation["id"]))["summary"]["parsed"] == 1
    )


async def test_parse_deadline_kills_process_and_quarantines(
    store: FinanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实长任务超过10秒被杀死，业务项持久隔离为PARSE_TIMEOUT。"""
    import os
    import time

    from anyio import to_process

    actual = to_process.run_sync
    pids: list[int] = []

    async def slow_parse(function: object, *args: object, **kwargs: object) -> object:
        """用真实30秒子进程任务注入解析卡顿，不改变生产10秒上限。"""
        pids.append(await actual(os.getpid, cancellable=True))
        return await actual(time.sleep, 30, **kwargs)

    monkeypatch.setattr(to_process, "run_sync", slow_parse)
    await prepare(store)
    operation = await store.accept_import(WHO, "company-a", [ref("slow", SAMPLE)], "parse-timeout")
    started = time.monotonic()
    await FinanceWorker(
        store, EncryptedLocalObjects(tmp_path, b"k" * 32), Gateway({"slow": SAMPLE}), synthetic=True
    ).run_once()
    result = await store.get_operation(WHO, operation["id"])
    assert (
        result["summary"]["quarantined"] == 1
        and result["items"][0]["error_code"] == "PARSE_TIMEOUT"
    )
    assert time.monotonic() - started < 20 and len(pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


async def test_date_aliases_cannot_create_multiple_main_invoices(
    store: FinanceStore, tmp_path: Path
) -> None:
    """同票同真实日期的紧凑/周日期别名隔离，主票始终只有一张。"""
    await prepare(store)
    objects = EncryptedLocalObjects(tmp_path, b"k" * 32)
    for name, data in (
        ("canonical", SAMPLE),
        ("compact", SAMPLE.replace(b"2026-10-21", b"20261021")),
        ("week", SAMPLE.replace(b"2026-10-21", b"2026-W43-3")),
    ):
        operation = await store.accept_import(WHO, "company-a", [ref(name, data)], "date-" + name)
        await FinanceWorker(store, objects, Gateway({name: data}), synthetic=True).run_once()
        result = await store.get_operation(WHO, operation["id"])
        if name == "canonical":
            assert result["summary"]["parsed"] == 1
        else:
            assert (
                result["summary"]["quarantined"] == 1
                and result["items"][0]["error_code"] == "PARSER_VALUE_INVALID"
            )
    assert (await store.list_invoices(WHO, "company-a"))["total"] == 1

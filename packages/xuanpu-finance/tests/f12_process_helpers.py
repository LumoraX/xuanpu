"""F12真实进程故障、网络边界和只读数据库证据辅助。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import asyncpg
import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request, Response
from xuanpu_finance.maintenance import MaintenanceOptions
from xuanpu_finance.objects import EncryptedLocalObjects

from .f10_recovery_helpers import recovery_config
from .f11_process_helpers import approve, port_closed, read_original
from .f11_verify_helpers import cli, snapshot
from .test_f0_e2e import SignedClient
from .test_f5_archive_formats import actual_pdf
from .test_f7_process import action, call
from .test_process import free_port
from .test_storage import WHO
from .test_worker import ref

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from xuanpu_finance.config import FinanceConfig
    from xuanpu_finance.storage import FinanceStore

EXECUTIONS = '/v1/finance/action-executions'
RECEIPT_LOCK = 617472012


@dataclass
class ProcessCase:
    """一例独立public库、网关和真实CLI配置，不持有任何产品替身。"""

    store: FinanceStore
    path: Path
    config: FinanceConfig
    signed: SignedClient
    raw: bytes
    payloads: dict[str, bytes]
    downloads: list[str]
    gateway_port: int
    outcomes: list[dict[str, Any]] = field(default_factory=list)


@asynccontextmanager
async def loopback(app: FastAPI) -> AsyncIterator[str]:
    """只监听本机，取消或断言失败时也等待HTTP服务退出。"""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(
        app, host='127.0.0.1', port=port, log_level='critical', access_log=False))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(.01)
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        assert port_closed(port)


@asynccontextmanager
async def counted_gateway(payloads: dict[str, bytes], downloads: list[str]) -> AsyncIterator[str]:
    """沿用F7真实loopback身份及文件边界，记录包括失败在内的每次下载请求。"""
    app = FastAPI()

    @app.get('/v1/integration-gateway/identity')
    async def identity(request: Request) -> dict[str, Any]:
        """回显实际调用员工，授权仍由产品签名验证及数据库裁决。"""
        return {'tenant_id': WHO.tenant_id,
                'user_id': request.headers['X-Xuanpu-Act-As'], 'active': True}

    @app.get('/v1/integration-gateway/files/{identifier}')
    async def file(identifier: str) -> Response:
        """删除源后返回404且照样计数，防止隐藏恢复时重新下载。"""
        downloads.append(identifier)
        if identifier not in payloads:
            return Response(status_code=404)
        return Response(payloads[identifier], media_type='application/pdf')

    async with loopback(app) as address:
        yield address


@asynccontextmanager
async def process_case(database: Any, directory: Path) -> AsyncIterator[ProcessCase]:
    """每例复用F4两新库，只在第一库public组装F10测试环境两级审批配置。"""
    store, _, dsns = database
    assert len(dsns) == 2 and dsns[0] != dsns[1]
    assert all(not urlsplit(dsn).query for dsn in dsns)
    assert await store.pool.fetchval('SELECT current_schema()') == 'public'
    raw, key, downloads = actual_pdf(), Ed25519PrivateKey.generate(), []
    payloads = {'pdf': raw}
    async with counted_gateway(payloads, downloads) as gateway:
        path, config = recovery_config(directory, dsns[0], gateway, key)
        assert config.environment == 'test' and config.profile == 'internal_records'
        for credential in [config.database_dsn_file, config.service_token_file, config.objects.key_file,
                           *config.public_key_files.values(), *config.gateway.tenant_token_files.values()]:
            credential.chmod(0o600)
        gateway_port = urlsplit(gateway).port
        assert gateway_port is not None
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{config.port}') as http:
            yield ProcessCase(store, path, config, SignedClient(http, key), raw,
                              payloads, downloads, gateway_port)
    assert port_closed(config.port)


@asynccontextmanager
async def killable_process(path: Path) -> AsyncIterator[asyncio.subprocess.Process]:
    """仅管理本轮创建的产品子进程，失败或取消时也回收这个PID。"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'xuanpu_finance', '--config', str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        yield process
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.communicate(), 15)


@dataclass
class ReceiptBarrier:
    """独立PG会话持有测试锁；另一连接只安装和删除测试触发器。"""

    guard: asyncpg.Connection
    admin: asyncpg.Connection
    backend: int | None = None
    released: bool = False

    async def release(self) -> None:
        """SIGKILL后立即放行，让服务器端死连接完成回滚并退出。"""
        if not self.released:
            assert await self.guard.fetchval('SELECT pg_advisory_unlock($1)', RECEIPT_LOCK)
            self.released = True
        if self.backend is not None:
            async with asyncio.timeout(10):
                while await self.admin.fetchval(
                    'SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)', self.backend):
                    await asyncio.sleep(.05)


@asynccontextmanager
async def receipt_barrier(dsn: str) -> AsyncIterator[ReceiptBarrier]:
    """仅测试库安装BEFORE INSERT阻塞，不修改产品函数或任何业务数据。"""
    guard, admin = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
    barrier = ReceiptBarrier(guard, admin)
    try:
        await guard.execute('SELECT pg_advisory_lock($1)', RECEIPT_LOCK)
        await admin.execute(f'''
            CREATE FUNCTION f12_pause_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM pg_advisory_xact_lock({RECEIPT_LOCK}); RETURN NEW; END $$;
            CREATE TRIGGER f12_pause_receipt BEFORE INSERT ON finance_receipt
            FOR EACH ROW EXECUTE FUNCTION f12_pause_receipt();
        ''')
        yield barrier
    finally:
        try:
            await barrier.release()
            await admin.execute('DROP TRIGGER IF EXISTS f12_pause_receipt ON finance_receipt; '
                                'DROP FUNCTION IF EXISTS f12_pause_receipt()')
        finally:
            await guard.close()
            await admin.close()


async def prepared_checkpoint(case: ProcessCase, barrier: ReceiptBarrier,
                              operation: dict[str, Any]) -> dict[str, Any]:
    """验证实际worker在密文写后、回执事务提交前等待，并保存原始租约时间。"""
    async with asyncio.timeout(10):
        while barrier.backend is None:
            barrier.backend = await barrier.admin.fetchval(
                'SELECT pid FROM pg_stat_activity WHERE $1=ANY(pg_blocking_pids(pid)) '
                'AND datname=current_database()', barrier.guard.get_server_pid())
            if barrier.backend is None:
                await asyncio.sleep(.02)
    row = await case.store.pool.fetchrow(
        'SELECT o.status,o.lease_until,o.bytes_received,i.*,now() AS db_now '
        'FROM finance_operation o JOIN finance_item i ON i.operation_id=o.id WHERE o.id=$1',
        operation['id'])
    assert row is not None
    assert row['status'] == 'running' and row['state'] == 'pending'
    assert row['receipt_id'] is None and row['object_key'] is not None
    assert row['bytes_received'] == 0
    assert 45 < (row['lease_until'] - row['db_now']).total_seconds() <= 60
    assert await case.store.pool.fetchval('SELECT count(*) FROM finance_receipt') == 0
    objects = EncryptedLocalObjects(case.config.objects.root, case.config.objects.key_file.read_bytes())
    authenticated = await objects.get(row['object_key'])
    assert authenticated == case.raw
    assert row['actual_sha256'] == hashlib.sha256(case.raw).hexdigest()
    kinds = await event_counts(case.store, operation['id'])
    assert kinds.get('original.prepared') == 1 and 'original.received' not in kinds
    claimed_at = await case.store.pool.fetchval(
        "SELECT created_at FROM finance_event WHERE aggregate_id=$1 AND kind='import.claimed'",
        operation['id'])
    assert (row['lease_until']-claimed_at).total_seconds() == 60
    return {'lease_until': row['lease_until'], 'observed_at': row['db_now'], 'claimed_at': claimed_at,
            'backend': barrier.backend, 'sha256': row['actual_sha256'],
            'cipher_authenticated': authenticated == case.raw, 'receipt_count': 0, 'item_state': row['state']}


async def instance_lock_available(dsn: str) -> bool:
    """用专用会话实际取得并释放产品实例锁，证明崩溃连接已释放它。"""
    connection = await asyncpg.connect(dsn)
    try:
        available = await connection.fetchval('SELECT pg_try_advisory_lock(617472002)')
        if available:
            assert await connection.fetchval('SELECT pg_advisory_unlock(617472002)')
        return bool(available)
    finally:
        await connection.close()


async def kill_at_checkpoint(case: ProcessCase, process: asyncio.subprocess.Process,
                             barrier: ReceiptBarrier, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """只对当前子进程PID发送SIGKILL，随后立即解除测试阻塞并验证清理。"""
    process.kill()
    stdout, _ = await asyncio.wait_for(process.communicate(), 10)
    await barrier.release()
    assert process.returncode == -signal.SIGKILL and stdout == b''
    assert port_closed(case.config.port)
    lock_available = await instance_lock_available(case.config.database_dsn_file.read_text())
    assert lock_available
    row = await case.store.pool.fetchrow(
        'SELECT lease_until,bytes_received FROM finance_operation WHERE id='
        '(SELECT operation_id FROM finance_item WHERE object_key IS NOT NULL LIMIT 1)')
    assert row is not None and row['lease_until'] == checkpoint['lease_until']
    assert row['bytes_received'] == 0
    return {'pid': process.pid, 'signal_exit': process.returncode,
            'port_closed': port_closed(case.config.port), 'instance_lock_released': lock_available,
            'lease_unchanged_after_crash': row['lease_until'] == checkpoint['lease_until']}


async def barrier_clean(store: FinanceStore) -> dict[str, bool]:
    """确认测试触发器、函数及数据库作用域的测试锁均已删除。"""
    clean = {
        'trigger_absent': not await store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='f12_pause_receipt')"),
        'function_absent': not await store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_proc WHERE proname='f12_pause_receipt')"),
        'guard_lock_absent': not await store.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND objid=$1 "
            "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()))", RECEIPT_LOCK)}
    assert all(clean.values())
    return clean


def import_body(raw: bytes) -> dict[str, Any]:
    """使用实际PDF的真实摘要与大小构造公开收件请求。"""
    return {'legal_entity_id': 'company-a', 'files': [ref('pdf', raw).model_dump()]}


async def wait_recovered(case: ProcessCase, operation: dict[str, Any],
                         checkpoint: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """短异步轮询最多95秒，逐次核对原租约未被测试改写且成功发生在到期之后。"""
    start, unchanged, pending = time.monotonic(), 0, []
    async with asyncio.timeout(95):
        while True:
            result = await call(case.signed, 'GET', '/v1/finance/operations/'+operation['id'])
            row = await case.store.pool.fetchrow(
                'SELECT lease_until,bytes_received,now() AS db_now FROM finance_operation WHERE id=$1',
                operation['id'])
            assert row is not None
            if row['db_now'] < checkpoint['lease_until']:
                assert row['lease_until'] == checkpoint['lease_until']
                assert result['status'] == 'running' and row['bytes_received'] == 0
                unchanged += 1
            if result['status'] == 'succeeded':
                assert row['db_now'] >= checkpoint['lease_until'] and unchanged > 0
                assert row['bytes_received'] == len(case.raw)
                break
            pending.append(result['status'])
            await asyncio.sleep(.15)
    elapsed = (row['db_now'] - checkpoint['claimed_at']).total_seconds()
    assert elapsed >= 60
    return result, {'recovery_poll_seconds': time.monotonic()-start,
                    'natural_expiry_elapsed_seconds': elapsed, 'lease_unchanged_polls': unchanged,
                    'pending_statuses': sorted(set(pending)), 'bytes_received': row['bytes_received'],
                    'claimed_at': checkpoint['claimed_at'].isoformat(),
                    'checkpoint_observed_at': checkpoint['observed_at'].isoformat(),
                    'lease_until': checkpoint['lease_until'].isoformat(),
                    'recovered_at': row['db_now'].isoformat()}


async def event_counts(store: FinanceStore, identifier: str) -> dict[str, int]:
    """从真实事件表聚合每种业务事件次数。"""
    rows = await store.pool.fetch(
        'SELECT kind,count(*) AS total FROM finance_event WHERE aggregate_id=$1 GROUP BY kind', identifier)
    return {row['kind']: row['total'] for row in rows}


async def draft_from_receipt(case: ProcessCase, receipt: str) -> dict[str, Any]:
    """本人用实际回执创建含原文的草稿，再通过真人预检及确认提交。"""
    doc = await call(case.signed, 'POST', '/v1/finance/expense-claims', idem='record', body={
        'legal_entity_id': 'company-a', 'reason': '交通报销',
        'invoice_information': '  票号TEST-ONLY\n金额10.00 ',
        'lines': [{'category_id': 'travel', 'amount': '10.00',
                   'occurred_on': '2026-10-22', 'reason': '地铁'}], 'evidence_ids': [receipt]})
    await action(case.signed, doc, 'submit_claim', WHO.user_id)
    return doc


async def retained_approval(case: ProcessCase, doc: dict[str, Any]) -> dict[str, Any]:
    """两级真实human确认后，原文、原件及零支付入账和外部表边界保持。"""
    prefix = '/v1/finance/expense-claims/'+doc['id']
    approvals = [await approve(case.signed, prefix, user, case.raw, reject_model=False)
                 for user in ['manager', 'finance-reviewer']]
    done = await call(case.signed, 'GET', prefix)
    retained = await read_original(case.signed, prefix, done, 'finance-reviewer', case.raw)
    assert done['status'] == 'approved'
    assert done['content']['invoice_information'] == '  票号TEST-ONLY\n金额10.00 '
    assert done['content']['reason'] == '交通报销'
    assert done['paid_amount'] == done['posted_amount'] == '0.00'
    decisions = await document_decisions(case.store, doc['id'])
    assert [decision['actor_id'] for decision in decisions] == ['manager', 'finance-reviewer']
    assert all(decision['data']['origin'] == 'human' for decision in decisions)
    return {'approvals': approvals, 'decisions': decisions, 'original': retained,
            'status': done['status'], 'paid_amount': done['paid_amount'],
            'posted_amount': done['posted_amount'], 'source_text_preserved': True,
            'external_table_counts': await external_counts(case.store)}


async def external_counts(store: FinanceStore) -> dict[str, int]:
    """仅统计固定外部业务表，内部记录验收不产生发票、支付或合规操作。"""
    counts = {table: await store.pool.fetchval('SELECT count(*) FROM '+table) for table in
              ['finance_invoice', 'finance_settlement_operation', 'finance_compliance_operation']}
    assert set(counts.values()) == {0}
    return counts


async def readonly_verify(case: ProcessCase) -> dict[str, Any]:
    """真实停机维护CLI的完整十字段结果与全库、逐文件密文快照同时核对。"""
    options = MaintenanceOptions(case.config.database_dsn_file, case.config.objects.root,
                                 case.config.objects.key_file)
    before = await snapshot(case.store, case.config.objects.root)
    result = await cli(options)
    equal = before == await snapshot(case.store, case.config.objects.root)
    assert result == (0, {'status': 'completed', 'verification_status': 'complete',
        'profile': 'internal_records', 'production_status': 'not_verified', 'authenticated_objects': 1,
        'prepared_missing_count': 0, 'unmapped_objects_count': 0, 'complete_object_validation': True,
        'workers_started': False, 'business_writes': 0}, ''), result
    assert equal
    return {'exit': result[0], 'output': result[1], 'snapshot_equal': equal}


async def document_decisions(store: FinanceStore, document: str) -> list[dict[str, Any]]:
    """按工作流关联实际人工决定，保留计划和来源，不保存原文正文。"""
    rows = await store.pool.fetch(
        'SELECT d.id,d.actor_id,d.action,d.step_id,d.data FROM finance_decision d '
        'JOIN finance_workflow w ON w.id=d.workflow_id WHERE w.document_id=$1 '
        'ORDER BY d.created_at,d.id', document)
    return [{**dict(row), 'data': json.loads(row['data'])} for row in rows]


async def approval_plan(case: ProcessCase, document: str, action_name: str = 'approve') -> dict[str, Any]:
    """当前负责人读取真实原件后，以当前单据和步骤版本取得可执行计划。"""
    prefix = '/v1/finance/expense-claims/'+document
    doc = await call(case.signed, 'GET', prefix, user='manager')
    await read_original(case.signed, prefix, doc, 'manager', case.raw)
    plan = await call(case.signed, 'POST', '/v1/finance/action-previews', user='manager',
                      idem='preview-'+action_name+document, body={
                          'action': action_name, 'target_type': 'expense_claim', 'target_id': document,
                          'expected_version': doc['version'], 'parameters': {
                              'workflow_step_id': doc['workflow']['current_step_id'],
                              'reason': '本人已核对独立原件'}})
    assert plan['executable'], plan['blockers']
    return plan


def execution_body(plan: dict[str, Any]) -> dict[str, str]:
    """只发送实际预检返回的计划ID与摘要。"""
    return {'plan_id': plan['id'], 'plan_digest': plan['digest']}


def request_identity(response: httpx.Response, signed: SignedClient) -> dict[str, str]:
    """验证实际请求EdDSA签名，记录来源与nonce以证明并发请求逐一签名。"""
    claims = jwt.decode(response.request.headers['X-Xuanpu-Context'], signed.key.public_key(),
                        algorithms=['EdDSA'], audience='finance', issuer='platform')
    return {**{key: str(claims[key]) for key in ['origin', 'sub', 'jti', 'call_id', 'request_sha256']},
            'idempotency_key': response.request.headers['Idempotency-Key'],
            'body_sha256': hashlib.sha256(response.request.content).hexdigest()}


async def workflow_facts(case: ProcessCase, document: str) -> dict[str, Any]:
    """读取真实单据、工作流、步骤状态和版本，不靠HTTP表面推断推进次数。"""
    claim = await case.store.pool.fetchrow(
        'SELECT version,status FROM finance_expense_claim WHERE id=$1', document)
    workflow = await case.store.pool.fetchrow(
        'SELECT id,version,status FROM finance_workflow WHERE document_id=$1', document)
    assert claim is not None and workflow is not None
    steps = await case.store.pool.fetch(
        'SELECT id,version,status,approver_user_id FROM finance_workflow_step '
        'WHERE workflow_id=$1 ORDER BY position', workflow['id'])
    return {'claim': dict(claim), 'workflow': dict(workflow), 'steps': [dict(row) for row in steps]}


async def preserved_record(case: ProcessCase, document: str) -> dict[str, Any]:
    """从当前HTTP单据核对原文和零支付入账，属性中只保存比较结果与金额。"""
    done = await call(case.signed, 'GET', '/v1/finance/expense-claims/'+document)
    text_equal = done['content']['invoice_information'] == '  票号TEST-ONLY\n金额10.00 '
    reason_equal = done['content']['reason'] == '交通报销'
    assert text_equal and reason_equal
    assert done['paid_amount'] == done['posted_amount'] == '0.00'
    return {'source_text_preserved': text_equal, 'reason_preserved': reason_equal,
            'paid_amount': done['paid_amount'], 'posted_amount': done['posted_amount']}


async def single_execution(case: ProcessCase, document: str, plan: dict[str, Any],
                            operation: str) -> dict[str, Any]:
    """同计划真正只写一份决定、本地操作与outbox，返回实际关联ID。"""
    decisions = await document_decisions(case.store, document)
    selected = [item for item in decisions if item['data']['plan_id'] == plan['id']]
    assert len(selected) == 1 and selected[0]['data']['origin'] == 'human'
    rows = await case.store.pool.fetch(
        'SELECT id FROM finance_business_operation WHERE plan_id=$1', plan['id'])
    outbox = await case.store.pool.fetch(
        'SELECT id FROM finance_outbox WHERE operation_id=$1', operation)
    assert [row['id'] for row in rows] == [operation] and len(outbox) == 1
    return {'decision': selected[0], 'operation_id': rows[0]['id'],
            'decision_count': len(selected), 'operation_count': len(rows),
            'outbox_count': len(outbox), 'outbox_id': outbox[0]['id']}


def assert_manager_advance(before: dict[str, Any], after: dict[str, Any]) -> None:
    """中间批准只推进一次流程与两个步骤版本，单据版本按产品契约保持。"""
    assert after['claim'] == before['claim']
    assert after['workflow']['id'] == before['workflow']['id']
    assert after['workflow']['version'] == before['workflow']['version']+1
    assert after['workflow']['status'] == 'under_review'
    assert [step['status'] for step in after['steps']] == ['approved', 'pending']
    assert [step['id'] for step in after['steps']] == [step['id'] for step in before['steps']]
    assert [step['version'] for step in after['steps']] == [
        step['version']+1 for step in before['steps']]


@asynccontextmanager
async def lost_response_proxy(upstream: str, observations: list[dict[str, Any]]) -> AsyncIterator[str]:
    """真实转发一次原始字节和签名headers，上游提交202后只丢弃响应交付。"""
    app = FastAPI()

    @app.post(EXECUTIONS)
    async def execute(request: Request) -> Response:
        """HTTP服务接收实际human请求；不解析重编码被签名绑定的正文。"""
        assert observations == [], '丢失响应代理只允许一次转发'
        raw = await request.body()
        headers = {name: request.headers[name] for name in
                   ['Authorization', 'X-Xuanpu-Context', 'Idempotency-Key', 'Content-Type']}
        async with httpx.AsyncClient(base_url=upstream) as http:
            response = await http.post(EXECUTIONS, content=raw, headers=headers)
        assert response.status_code == 202, response.text
        observations.append({'upstream_status': response.status_code,
            'operation_id': response.json()['id'], 'body_sha256': hashlib.sha256(raw).hexdigest(),
            'forwarded_body_equal': response.request.content == raw,
            'context_header_equal': response.request.headers['X-Xuanpu-Context'] == headers['X-Xuanpu-Context'],
            'idempotency_header_equal': response.request.headers['Idempotency-Key'] == headers['Idempotency-Key']})
        return Response(content=b'{"code":"RESPONSE_DELIVERY_LOST"}',
                        status_code=503, media_type='application/json')

    async with loopback(app) as address:
        yield address


async def final_human_review(case: ProcessCase, document: str) -> dict[str, Any]:
    """末级仍由真实human签名确认，批准后核对原文原件和外部零写入。"""
    prefix = '/v1/finance/expense-claims/'+document
    before = await workflow_facts(case, document)
    approval = await approve(case.signed, prefix, 'finance-reviewer', case.raw, reject_model=False)
    after = await workflow_facts(case, document)
    assert after['claim']['version'] == before['claim']['version']+1
    assert after['claim']['status'] == after['workflow']['status'] == 'approved'
    done = await call(case.signed, 'GET', prefix)
    assert done['content']['invoice_information'] == '  票号TEST-ONLY\n金额10.00 '
    assert done['content']['reason'] == '交通报销'
    assert done['paid_amount'] == done['posted_amount'] == '0.00'
    assert approval['signed_origin'] == 'human'
    return {'approval': approval, 'before': before, 'after': after,
            'paid_amount': done['paid_amount'], 'posted_amount': done['posted_amount'],
            'source_text_preserved': True, 'external_table_counts': await external_counts(case.store)}

"""F13真实CLI文件输出、目录拒绝及签名安全记账证据。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import asyncpg
import jwt
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_spec.call_context import request_fingerprint

from .f11_process_helpers import approve, port_closed, read_original
from .f11_verify_helpers import snapshot
from .f12_process_helpers import (
    document_decisions,
    external_counts,
    preserved_record,
    single_execution,
)
from .test_f0_e2e import SignedClient
from .test_f7_process import call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import httpx

    from .f12_process_helpers import ProcessCase


@asynccontextmanager
async def observed_file_process(
    path: Path, outcomes: list[dict[str, Any]], port: int,
) -> AsyncIterator[None]:
    """分别以临时文件消费两路输出，失败时SIGTERM后有界强制回收自建进程。"""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'xuanpu_finance', '--config', str(path),
            stdout=output, stderr=error)
        forced = False
        try:
            yield
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                forced = True
                process.kill()
                await asyncio.wait_for(process.wait(), 15)
            output.seek(0)
            error.seek(0)
            stdout, stderr = output.read(), error.read()
            result = {'exit': process.returncode, 'ended': process.returncode is not None,
                'forced_kill': forced, 'graceful_shutdown': b'Application shutdown complete.' in stderr,
                'stdout_empty': stdout == b'', 'stderr_bytes': len(stderr),
                'stderr_sha256': hashlib.sha256(stderr).hexdigest(), 'port_closed': port_closed(port)}
            outcomes.append(result)
            assert process.returncode == -signal.SIGTERM and not forced, result
            assert result['graceful_shutdown'] and result['stdout_empty'] and result['port_closed'], result


def signed_request(response: httpx.Response, signed: SignedClient) -> dict[str, Any]:
    """验证实际EdDSA请求并只保存安全字段，绝不保存令牌或请求正文。"""
    claims = jwt.decode(response.request.headers['X-Xuanpu-Context'], signed.key.public_key(),
                        algorithms=['EdDSA'], audience='finance', issuer='platform')
    request = response.request
    fingerprint = request_fingerprint(request.method, request.url.path,
        list(request.url.params.multi_items()), json.loads(request.content) if request.content else None,
        request.headers.get('Idempotency-Key', ''), request.headers.get('Content-Type', ''))
    assert claims['request_sha256'] == fingerprint
    return {key: claims[key] for key in
            ['iss', 'sub', 'tenant_id', 'origin', 'jti', 'call_id', 'request_sha256', 'exp']} | {
                'idempotency_key': response.request.headers.get('Idempotency-Key', '')}


class RecordedSignedClient(SignedClient):
    """沿用真实签名客户端，仅记录真实确认请求的已验证身份字段。"""

    def __init__(self, signed: SignedClient) -> None:
        """复用既有真实HTTP连接和本轮密钥，单独保留确认证据。"""
        super().__init__(signed.client, signed.key)
        self.executions: list[dict[str, Any]] = []

    async def call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """不替代请求或响应，真实确认完成后关联实际操作ID和HTTP状态。"""
        response = await super().call(method, path, **kwargs)
        if path == '/v1/finance/action-executions':
            result = response.json()
            self.executions.append({'signed': signed_request(response, self),
                'status': response.status_code, 'operation_id': result.get('id'),
                'code': result.get('code')})
        return response


def ciphertext_files(directory: Path) -> dict[str, bytes]:
    """精确比较独占外部目录中的密文字节，不将内容写入报告。"""
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob('*') if path.is_file()}


async def receipt_ciphertext(case: ProcessCase, doc: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    """从实际收件回执查对象键及原操作，以既有哈希布局定位当前独占密文。"""
    receipt = await case.store.pool.fetchrow(
        'SELECT id,object_key,sha256,operation_id FROM finance_receipt WHERE id=$1',
        doc['content']['evidence_ids'][0])
    assert receipt is not None
    operation = await case.store.pool.fetchrow(
        'SELECT id,status FROM finance_operation WHERE id=$1', receipt['operation_id'])
    assert operation is not None and operation['status'] == 'succeeded'
    objects = EncryptedLocalObjects(case.config.objects.root, case.config.objects.key_file.read_bytes())
    path = objects._path(receipt['object_key'])
    assert path.is_file() and not path.is_symlink()
    return path, {'receipt_id': receipt['id'], 'original_operation': dict(operation),
                  'sha256': receipt['sha256']}


async def replay_rows(connection: asyncpg.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """读取防重放全部实际字段，逐行核对新增、移除及既有内容。"""
    rows = await connection.fetch('SELECT * FROM finance_context_replay ORDER BY issuer,jti')
    return {(row['issuer'], row['jti']): dict(row) for row in rows}


def replay_changes(
    before: dict[Any, Any], after: dict[Any, Any], identity: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """仅允许本次真实签名新增一行以及已实际到期的清理，不隐藏既有行改写。"""
    added, removed = after.keys()-before.keys(), before.keys()-after.keys()
    assert added == {(identity['iss'], identity['jti'])}
    assert all(before[key] == after[key] for key in before.keys() & after.keys())
    assert all(before[key]['expires_at'] < now for key in removed)
    actual = after[next(iter(added))]
    expected = {'issuer': identity['iss'], 'jti': identity['jti'],
        'subject': json.dumps([identity['tenant_id'], identity['sub'], identity['origin']],
                              ensure_ascii=False, separators=(',', ':')),
        'request_hash': identity['request_sha256'], 'idempotency_key': identity['idempotency_key'],
        'expires_at': datetime.fromtimestamp(identity['exp'], UTC)}
    assert actual == expected
    return {'actual_signed_request': identity, 'added_count': len(added),
            'added_matches_signature': actual == expected, 'removed_count': len(removed),
            'removed_were_expired': all(before[key]['expires_at'] < now for key in removed),
            'existing_rows_unchanged': True}


async def refused_original(
    case: ProcessCase, material_url: str, outside: Path,
) -> dict[str, Any]:
    """独立事务持通知锁，真实签名拒绝前后逐表比较并单独核对安全记账。"""
    guard = await asyncpg.connect(case.config.database_dsn_file.read_text())
    try:
        async with guard.transaction():
            await guard.execute('SELECT pg_advisory_xact_lock(617472004)')
            before, _ = await snapshot(case.store, case.config.objects.root)
            replay_before, files_before = await replay_rows(guard), ciphertext_files(outside)
            response = await case.signed.call('GET', material_url, user='manager', origin='human')
            assert response.status_code == 500, {'status': response.status_code,
                'code': response.json().get('code'),
                'content_base64_present': 'content_base64' in response.json()}
            assert response.json()['code'] == 'OBJECT_PATH_UNSAFE'
            assert b'content_base64' not in response.content
            assert case.raw not in response.content
            assert str(outside).encode() not in response.content
            assert str(case.config.objects.root).encode() not in response.content
            after, _ = await snapshot(case.store, case.config.objects.root)
            replay_after = await replay_rows(guard)
            replay = replay_changes(replay_before, replay_after, signed_request(response, case.signed),
                                    await guard.fetchval('SELECT clock_timestamp()'))
            # 防重放是唯一例外；其它每一张public表均保留精确对比。
            assert set(before) == set(after) and 'finance_context_replay' in before
            tables = [name for name in before if name != 'finance_context_replay']
            equal = {name: before[name] == after[name] for name in tables}
            assert all(equal.values()), equal
            external_equal = files_before == ciphertext_files(outside)
            assert external_equal
            return {'status': response.status_code, 'code': response.json()['code'],
                'content_base64_absent': True, 'path_and_body_absent': True,
                'public_tables_equal': equal, 'external_ciphertext_unchanged': external_equal,
                'replay': replay}
    finally:
        await guard.close()


async def two_human_approvals(case: ProcessCase, doc: dict[str, Any]) -> dict[str, Any]:
    """两级均先验证agent真实403，再由human签名确认，数据库核对实际决定。"""
    prefix = '/v1/finance/expense-claims/'+doc['id']
    approvals = [await approve(case.signed, prefix, user, case.raw, reject_model=True)
                 for user in ['manager', 'finance-reviewer']]
    done = await call(case.signed, 'GET', prefix)
    decisions = await document_decisions(case.store, doc['id'])
    assert done['status'] == 'approved'
    assert [decision['actor_id'] for decision in decisions] == ['manager', 'finance-reviewer']
    assert all(decision['data']['origin'] == 'human' for decision in decisions)
    assert {decision['data']['parameters']['reason'] for decision in decisions} == {
        approval['reason'] for approval in approvals}
    operations = [await single_execution(case, doc['id'], {'id': decision['data']['plan_id']},
                   approval['human_execution']['id'])
                  for decision, approval in zip(decisions, approvals, strict=True)]
    retained = await read_original(case.signed, prefix, done, 'finance-reviewer', case.raw)
    return {'approvals': approvals, 'decisions': decisions, 'operations': operations,
            'original': retained,
            'status': done['status'],
            'record': await preserved_record(case, doc['id']),
            'external_table_counts': await external_counts(case.store)}

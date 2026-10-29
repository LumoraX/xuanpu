"""F11独立public库实际CLI、HTTP、两级真人与只读维护联合验收。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.maintenance import MaintenanceOptions

from .f10_recovery_helpers import recovery_config, write_config
from .f11_process_helpers import (
    approve,
    observed_process,
    port_closed,
    read_original,
    rejected_startup,
)
from .f11_verify_helpers import cli, snapshot
from .test_f0_e2e import SignedClient
from .test_f4_maintenance import maintenance_database as maintenance_database  # noqa: F401
from .test_f5_archive_formats import actual_pdf
from .test_f7_process import call, receive_and_submit, record_gateway
from .test_f11_startup import production_config
from .test_process import wait_for_ready

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_internal_records_real_process_and_readonly_verify(
    maintenance_database: Any, tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    """实际收件及两级决定跨一次进程重启，停机核验全表与密文均未改写。"""
    store, _, dsns = maintenance_database
    assert not urlsplit(dsns[0]).query
    assert await store.pool.fetchval('SELECT current_schema()') == 'public'
    raw, key, outcomes = actual_pdf(), Ed25519PrivateKey.generate(), []
    payloads = {'pdf': raw}
    approvals = []
    async with record_gateway(payloads) as gateway:
        gateway_port = urlsplit(gateway).port
        path, config = recovery_config(tmp_path/'process', dsns[0], gateway, key)
        assert config.environment == 'test' and config.profile == 'internal_records'
        for credential in [config.database_dsn_file, config.service_token_file, config.objects.key_file,
                           *config.public_key_files.values(), *config.gateway.tenant_token_files.values()]:
            credential.chmod(0o600)
        options = MaintenanceOptions(config.database_dsn_file, config.objects.root, config.objects.key_file)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{config.port}') as http:
            signed = SignedClient(http, key)
            async with observed_process(path, outcomes):
                await wait_for_ready(http)
                doc = await receive_and_submit(signed, raw)
                prefix = '/v1/finance/expense-claims/'+doc['id']
                active = await cli(options)
                assert active == (1, {'status': 'failed', 'code': 'INSTANCE_ACTIVE'}, '')
                approvals.append(await approve(signed, prefix, 'manager', raw, reject_model=True))
            assert port_closed(config.port)
            payloads.clear()
            async with observed_process(path, outcomes):
                await wait_for_ready(http)
                approvals.append(await approve(signed, prefix, 'finance-reviewer', raw, reject_model=False))
                done = await call(signed, 'GET', prefix)
                history = await call(signed, 'GET', prefix+'/history')
                retained = await read_original(signed, prefix, done, 'finance-reviewer', raw)
                assert done['status'] == 'approved'
                assert done['content']['invoice_information'] == '  票号TEST-ONLY\n金额10.00 '
                assert done['content']['reason'] == '交通报销'
                assert done['paid_amount'] == done['posted_amount'] == '0.00'
    assert gateway_port is not None and port_closed(gateway_port) and port_closed(config.port)
    counts = {table: await store.pool.fetchval('SELECT count(*) FROM '+table) for table in
              ['finance_invoice', 'finance_settlement_operation', 'finance_compliance_operation']}
    assert set(counts.values()) == {0}
    receipts = [dict(row) for row in await store.pool.fetch('SELECT id,sha256 FROM finance_receipt')]
    assert len(receipts) == 1 and receipts[0]['id'] == done['content']['evidence_ids'][0]
    assert receipts[0]['sha256'] == retained['sha256']
    decisions = [dict(row) for row in await store.pool.fetch(
        "SELECT actor_id,action,data FROM finance_decision ORDER BY created_at,id")]
    for decision in decisions:
        decision['data'] = json.loads(decision['data'])
    approved = [d for d in decisions if d['action'] == 'approve']
    assert [d['actor_id'] for d in approved] == ['manager', 'finance-reviewer']
    assert all(d['data']['origin'] == 'human' for d in approved)
    assert {d['data']['parameters']['reason'] for d in approved} == {a['reason'] for a in approvals}
    assert sum(item['kind'] == 'decision' for item in history['items']) == len(decisions)
    before = await snapshot(store, config.objects.root)
    stopped = await cli(options)
    equal = before == await snapshot(store, config.objects.root)
    assert stopped[0] == 0 and stopped[2] == '' and len(stopped[1]) == 10 and equal
    assert stopped[1]['authenticated_objects'] == 1 and stopped[1]['production_status'] == 'not_verified'
    assert stopped[1]['workers_started'] is False and stopped[1]['business_writes'] == 0
    assert stopped[1] == {'status': 'completed', 'verification_status': 'complete',
        'profile': 'internal_records', 'production_status': 'not_verified', 'authenticated_objects': 1,
        'prepared_missing_count': 0, 'unmapped_objects_count': 0, 'complete_object_validation': True,
        'workers_started': False, 'business_writes': 0}
    request.node.user_properties.append(('f11_process', json.dumps({
        'invoice_information_preserved': done['content']['invoice_information'] == '  票号TEST-ONLY\n金额10.00 ',
        'reason_preserved': done['content']['reason'] == '交通报销', 'status': done['status'],
        'paid_amount': done['paid_amount'], 'posted_amount': done['posted_amount'], 'original': retained,
        'approvals': approvals, 'decisions': decisions, 'receipts': receipts, 'external_table_counts': counts,
        'active_verify': {'exit': active[0], 'output': active[1]},
        'stopped_verify': {'exit': stopped[0], 'output': stopped[1]}, 'snapshot_equal': equal,
        'processes': outcomes, 'finance_port_closed': port_closed(config.port),
        'gateway_port_closed': port_closed(gateway_port)}, ensure_ascii=False)))


async def test_production_bad_permissions_real_cli_preserves_database(
    maintenance_database: Any, tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    """真实正式内部CLI权限拒绝不创建对象目录，独立库全部业务行保持原样。"""
    store, _, dsns = maintenance_database
    config = production_config(tmp_path/'production')
    config.database_dsn_file.write_text(dsns[0])
    assert config.environment == 'production' and config.profile == 'internal_records'
    assert not urlsplit(dsns[0]).query
    for credential in [config.database_dsn_file, config.objects.key_file,
                       *config.public_key_files.values(), *config.gateway.tenant_token_files.values()]:
        assert credential.stat().st_mode & 0o777 == 0o600
    config.service_token_file.chmod(0o644)
    path = tmp_path/'production.yaml'
    write_config(path, config)
    assert not config.objects.root.exists()
    before = await snapshot(store, config.objects.root)
    code, output, stderr = await rejected_startup(path)
    equal = before == await snapshot(store, config.objects.root)
    assert code == 2 and stderr == ''
    assert output == {'status': 'blocked', 'production_status': 'not_verified', 'code': 'INTERNAL_STARTUP_BLOCKED'}
    assert equal and not config.objects.root.exists()
    request.node.user_properties.append(('f11_process', json.dumps({
        'production_bad_permissions': {'exit': code, 'output': output, 'stderr_empty': stderr == ''},
        'object_root_absent': not config.objects.root.exists(), 'snapshot_equal': equal}, ensure_ascii=False)))

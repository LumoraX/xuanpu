"""F10真实维护CLI恢复后只读核对、撤权失效与两级人工续办。"""
from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import asyncpg
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.storage import FinanceStore

from .f10_recovery_helpers import (
    maintenance_cli,
    persisted_snapshot,
    recovery_config,
    write_config,
)
from .test_f0_e2e import SignedClient
from .test_f4_maintenance import maintenance_database as maintenance_database
from .test_f4_maintenance import pg_tools
from .test_f5_archive_formats import actual_pdf
from .test_f7_process import action, call, receive_and_submit, record_gateway
from .test_process import finance_process, wait_for_ready
from .test_storage import WHO

if TYPE_CHECKING:
    from pathlib import Path


async def test_real_cli_restored_internal_readonly_then_human_continuation(
    maintenance_database: Any, tmp_path: Path, request: Any,
) -> None:
    """两个真实库配套恢复，旧授权不复活，源字节删除后重提完成两级审批。"""
    _, options, dsns = maintenance_database
    raw, key = actual_pdf(), Ed25519PrivateKey.generate()
    payloads = {'pdf': raw}
    async with record_gateway(payloads) as gateway:
        path, config = recovery_config(tmp_path/'process', dsns[0], gateway, key)
        config.objects.root = options[0].object_root
        config.objects.key_file = options[0].key_file
        write_config(path, config)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{config.port}') as http:
            signed = SignedClient(http, key)
            async with finance_process(path):
                await wait_for_ready(http)
                doc = await receive_and_submit(signed, raw)
                initial = await call(signed, 'GET', '/v1/finance/internal-records-readiness',
                    user='finance-reviewer', params={'legal_entity_id': 'company-a'})
                assert initial['status'] == 'not_verified', initial
                current = await call(signed, 'GET', '/v1/finance/expense-claims/'+doc['id'])
                before_backup_plan = await preview_approval(signed, current)
                assert before_backup_plan['executable'] is True, before_backup_plan['blockers']
            before = await persisted_snapshot(dsns[0])
            dump, load = pg_tools(tmp_path)
            backup = await maintenance_cli('backup', options[0], tmp_path/'backup', dump)
            restored = await maintenance_cli('restore', options[1], tmp_path/'backup', load)
            assert backup['backup_complete'] and restored['database_restored']
            assert restored['workers_started'] is False and restored['external_side_effects'] == 0
            assert await persisted_snapshot(dsns[1]) == before
            payloads.clear()
            config.database_dsn_file = options[1].database_dsn_file
            config.objects.root, config.mode = options[1].object_root, 'read_only'
            write_config(path, config)
            await verify_readonly(signed, http, path, doc, raw, initial, before_backup_plan)
            assert await persisted_snapshot(dsns[1]) == before
            config.mode = 'enabled'
            write_config(path, config)
            async with finance_process(path):
                await wait_for_ready(http)
                doc = await revoke_and_continue(signed, dsns[1], doc)
            config.mode = 'disabled'
            write_config(path, config)
            async with finance_process(path):
                await wait_for_ready(http)
                disabled = await signed.call('GET', '/v1/finance/expense-claims/'+doc['id'])
                assert disabled.status_code == 503
    final = await persisted_snapshot(dsns[1])
    decisions = [d for d in final['finance_decision'] if d['action'] == 'approve']
    assert len(decisions) == 2 and {d['actor_id'] for d in decisions} == {'manager', 'finance-reviewer'}
    assert all(d['data']['origin'] == 'human' and d['actor_id'] != WHO.user_id for d in decisions)
    conn = await asyncpg.connect(dsns[1])
    try:
        for table in ['finance_invoice', 'finance_settlement_operation', 'finance_compliance_operation']:
            assert await conn.fetchval('SELECT count(*) FROM '+table) == 0
    finally:
        await conn.close()
    request.node.user_properties.append(('f10_recovery', json.dumps({'approved_id': doc['id'], 'version': doc['version'],
        'read_only_snapshot_equal': True, 'restored_configuration_digest_equal': True,
        'decision_count': 2, 'origin': 'human', 'external_operations': 0,
        'backup_complete': True, 'real_maintenance_cli': True}, sort_keys=True)))


async def verify_readonly(signed: SignedClient, http: httpx.AsyncClient, path: Path,
                         doc: dict[str, Any], raw: bytes, initial: dict[str, Any],
                         existing_plan: dict[str, Any]) -> None:
    """恢复只读启动原文、摘要、历史与原件可核对，业务写命令拒绝。"""
    async with finance_process(path):
        await wait_for_ready(http)
        report = await call(signed, 'GET', '/v1/finance/internal-records-readiness',
            user='finance-reviewer', params={'legal_entity_id': 'company-a'})
        assert report['status'] == report['production_status'] == 'not_verified'
        assert report['configuration_digest'] == initial['configuration_digest']
        assert report['runtime']['mode'] == 'read_only'
        prefix = '/v1/finance/expense-claims/'+doc['id']
        restored = await call(signed, 'GET', prefix)
        assert restored['content']['invoice_information'] == '  票号TEST-ONLY\n金额10.00 '
        receipt = restored['content']['evidence_ids'][0]
        material = await call(signed, 'GET', prefix+'/materials/'+receipt, user='manager')
        assert base64.b64decode(material['content_base64']) == raw
        history = await call(signed, 'GET', prefix+'/history')
        assert history['total'] >= 2
        write = await signed.call('POST', '/v1/finance/expense-claims', body=restored['content'],
            origin='human', idem='blocked-readonly')
        assert write.status_code == 409
        execution = await signed.call('POST', '/v1/finance/action-executions', user='manager',
            origin='human', idem='readonly-existing-plan',
            body={'plan_id': existing_plan['id'], 'plan_digest': existing_plan['digest']})
        assert execution.status_code == 409


async def revoke_and_continue(signed: SignedClient, dsn: str,
                              doc: dict[str, Any]) -> dict[str, Any]:
    """实际撤权和恢复后旧计划失效，申请人撤回重提才获得新分派资格。"""
    prefix = '/v1/finance/expense-claims/'+doc['id']
    current = await call(signed, 'GET', prefix)
    plan = await preview_approval(signed, current, key='before-revoke')
    assert plan['executable']
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    assert pool is not None
    store = FinanceStore(pool)
    try:
        await store.grant(WHO.tenant_id, 'manager', 'company-a', 'collector')
        denied = await signed.call('GET', prefix, user='manager')
        assert denied.status_code == 404
        await store.grant(WHO.tenant_id, 'manager', 'company-a', 'approver')
        old = await signed.call('POST', '/v1/finance/action-executions', user='manager',
            origin='human', idem='stale-after-restore',
            body={'plan_id': plan['id'], 'plan_digest': plan['digest']})
        assert old.status_code == 409
        owner = FinancePrincipal(WHO.tenant_id, WHO.user_id, 'human')
        assert (await store.get_expense_claim(owner, doc['id']))['status'] == 'under_review'
    finally:
        await pool.close()
    await action(signed, current, 'withdraw', WHO.user_id, reason='撤权后重新核定审批链')
    current = await call(signed, 'GET', prefix)
    await action(signed, current, 'submit_claim', WHO.user_id)
    for user in ['manager', 'finance-reviewer']:
        current = await call(signed, 'GET', prefix, user=user)
        await action(signed, current, 'approve', user,
            workflow_step_id=current['workflow']['current_step_id'], reason='恢复原件与岗位核对')
    result = await call(signed, 'GET', prefix)
    assert result['status'] == 'approved'
    return result


async def preview_approval(signed: SignedClient, doc: dict[str, Any],
                           *, key: str = 'before-backup') -> dict[str, Any]:
    """保留真正分派人的有效批准计划，供只读和授权失效测试。"""
    return await call(signed, 'POST', '/v1/finance/action-previews', user='manager',
        idem=key, body={'action': 'approve', 'target_type': 'expense_claim',
            'target_id': doc['id'], 'expected_version': doc['version'],
            'parameters': {'workflow_step_id': doc['workflow']['current_step_id']}})

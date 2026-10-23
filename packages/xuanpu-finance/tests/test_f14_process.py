"""F14真实维护凭据拒绝、独立配套恢复及目标库两级真人续办。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.maintenance import MaintenanceOptions

from .f10_recovery_helpers import write_config
from .f11_process_helpers import port_closed
from .f11_verify_helpers import snapshot
from .f12_process_helpers import process_case, readonly_verify
from .f13_process_helpers import RecordedSignedClient, observed_file_process, two_human_approvals
from .f14_process_helpers import original_identity, prepare_backup, receipt_state, target_store
from .test_f4_maintenance import maintenance_database as maintenance_database  # noqa: F401
from .test_f7_process import receive_and_submit
from .test_process import wait_for_ready

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_bad_credentials_refused_then_restored_target_humans_resume(
    maintenance_database: Any, tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    """真实CLI先拒绝八组坏凭据，合法备份恢复后目标库真人审批且停机verify零写。"""
    _, fixture_options, dsns = maintenance_database
    async with process_case(maintenance_database, tmp_path/'process') as case:
        case.config.objects.root = fixture_options[0].object_root
        case.config.objects.key_file = fixture_options[0].key_file
        assert case.config.objects.key_file.read_bytes() == fixture_options[1].key_file.read_bytes()
        write_config(case.path, case.config)
        case.signed = RecordedSignedClient(case.signed)
        async with observed_file_process(case.path, case.outcomes, case.config.port):
            await wait_for_ready(case.signed.client)
            doc = await receive_and_submit(case.signed, case.raw)
            original = await original_identity(case, doc)
            imported = await receipt_state(case, doc)
            case.payloads.clear()
            assert case.payloads == {} and case.downloads == ['pdf']
        options = [MaintenanceOptions(case.config.database_dsn_file, case.config.objects.root,
                                      case.config.objects.key_file), fixture_options[1]]
        maintenance = await prepare_backup(case, options, dsns, tmp_path/'maintenance')
        source_before = await snapshot(case.store, options[0].object_root)
        async with target_store(case, options[1], dsns[1]) as database:
            assert await receipt_state(case, doc) == imported
            assert await original_identity(case, doc) == original
            assert source_before == await snapshot(case.store, options[1].object_root)
            case.signed.executions.clear()
            async with observed_file_process(case.path, case.outcomes, case.config.port):
                await wait_for_ready(case.signed.client)
                final = await two_human_approvals(case, doc)
                assert [entry['status'] for entry in case.signed.executions] == [403, 202, 403, 202]
                assert [entry['signed']['origin'] for entry in case.signed.executions] == [
                    'agent', 'human', 'agent', 'human']
                assert case.payloads == {} and case.downloads == ['pdf']
                assert await receipt_state(case, doc) == imported
                retained = await original_identity(case, doc)
                assert retained == original
            verify = await readonly_verify(case)
        assert source_before == await snapshot(case.store, options[0].object_root)
        result = {'maintenance': maintenance, 'original': original, 'retained': retained,
            'original_import_and_receipt_equal': retained == original, 'database': database,
            'final': final, 'confirmation_requests': case.signed.executions,
            'source_removed': case.payloads == {}, 'download_count': len(case.downloads),
            'source_unchanged_during_target_approvals': True, 'readonly_verify': verify,
            'processes': case.outcomes, 'finance_port_closed': port_closed(case.config.port)}
    result['gateway_port_closed'] = port_closed(case.gateway_port)
    assert result['finance_port_closed'] and result['gateway_port_closed']
    request.node.user_properties.append(('f14_process', json.dumps(result, ensure_ascii=False)))

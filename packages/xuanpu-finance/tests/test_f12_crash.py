"""F12真实SIGKILL后保全原件、自然租约到期恢复与真人审批验收。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .f11_process_helpers import observed_process, port_closed
from .f12_process_helpers import (
    barrier_clean,
    draft_from_receipt,
    event_counts,
    import_body,
    kill_at_checkpoint,
    killable_process,
    prepared_checkpoint,
    process_case,
    readonly_verify,
    receipt_barrier,
    retained_approval,
    wait_recovered,
)
from .test_f4_maintenance import maintenance_database as maintenance_database  # noqa: F401
from .test_f7_process import call
from .test_process import wait_for_ready

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_sigkill_after_cipher_before_receipt_recovers_naturally(
    maintenance_database: Any, tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    """实际对象已写、回执未提交时杀进程，源消失后等待真实60秒租约恢复一次。"""
    async with process_case(maintenance_database, tmp_path/'crash') as case:
        dsn = case.config.database_dsn_file.read_text()
        async with receipt_barrier(dsn) as barrier:
            async with killable_process(case.path) as process:
                await wait_for_ready(case.signed.client)
                operation = await call(case.signed, 'POST', '/v1/finance/supporting-material-imports',
                                       idem='crash-receipt', body=import_body(case.raw))
                checkpoint = await prepared_checkpoint(case, barrier, operation)
                killed = await kill_at_checkpoint(case, process, barrier, checkpoint)
        cleaned = await barrier_clean(case.store)
        case.payloads.clear()
        assert case.payloads == {} and case.downloads == ['pdf']
        async with observed_process(case.path, case.outcomes):
            await wait_for_ready(case.signed.client)
            recovered, timing = await wait_recovered(case, operation, checkpoint)
            replay = await call(case.signed, 'POST', '/v1/finance/supporting-material-imports',
                                idem='crash-receipt', body=import_body(case.raw))
            assert replay['id'] == operation['id'] == recovered['id']
            assert case.downloads == ['pdf']
            receipts = await case.store.pool.fetch(
                'SELECT id,sha256 FROM finance_receipt WHERE operation_id=$1', operation['id'])
            assert len(receipts) == 1
            assert receipts[0]['id'] == recovered['result_refs'][0]['id']
            assert receipts[0]['sha256'] == checkpoint['sha256']
            counts = await event_counts(case.store, operation['id'])
            assert counts['original.received'] == counts['material.received'] == 1
            assert counts['original.prepared'] == 1
            doc = await draft_from_receipt(case, receipts[0]['id'])
            human = await retained_approval(case, doc)
        verified = await readonly_verify(case)
        assert port_closed(case.config.port)
    assert port_closed(case.gateway_port)
    request.node.user_properties.append(('f12_crash', json.dumps({
        'fault_boundary': 'cipher_authenticated_before_receipt_commit',
        'operation_id': operation['id'], 'replay_operation_id': replay['id'],
        'receipt_id': receipts[0]['id'], 'original_sha256': checkpoint['sha256'],
        'checkpoint': {key: checkpoint[key] for key in
                       ['backend', 'cipher_authenticated', 'receipt_count', 'item_state']},
        'crash': killed, 'timing': timing, 'cleanup': cleaned,
        'download_count': len(case.downloads), 'event_counts': counts,
        'human_approvals': human, 'verify': verified, 'processes': case.outcomes,
        'finance_port_closed': port_closed(case.config.port),
        'gateway_port_closed': port_closed(case.gateway_port)}, ensure_ascii=False)))

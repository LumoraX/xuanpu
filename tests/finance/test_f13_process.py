"""F13真实CLI拒绝越界原件、管理员恢复及两级人工续办联合验收。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .f11_process_helpers import port_closed, read_original
from .f12_process_helpers import process_case, readonly_verify
from .f13_process_helpers import (
    RecordedSignedClient,
    observed_file_process,
    receipt_ciphertext,
    refused_original,
    two_human_approvals,
)
from .test_f4_maintenance import maintenance_database as maintenance_database  # noqa: F401
from .test_f7_process import receive_and_submit
from .test_process import wait_for_ready

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_unsafe_original_refused_then_humans_resume_after_explicit_restore(
    maintenance_database: Any, tmp_path: Path, request: pytest.FixtureRequest,
) -> None:
    """源移除后链接拒绝无业务变更，恢复原密文后原操作两级真人批准且停机只读。"""
    async with process_case(maintenance_database, tmp_path/'process') as case:
        signed = RecordedSignedClient(case.signed)
        case.signed = signed
        async with observed_file_process(case.path, case.outcomes, case.config.port):
            await wait_for_ready(case.signed.client)
            doc = await receive_and_submit(case.signed, case.raw)
            case.payloads.clear()
            prefix = '/v1/finance/expense-claims/'+doc['id']
            original = await read_original(case.signed, prefix, doc, 'manager', case.raw)
            leaf, receipt = await receipt_ciphertext(case, doc)
            assert original['sha256'] == receipt['sha256']
            outside = tmp_path/'outside-original-root'
            assert not outside.is_relative_to(case.config.objects.root)
            outside.mkdir()
            moved = outside/leaf.name
            leaf.rename(moved)
            try:
                leaf.symlink_to(moved)
                refusal = await refused_original(
                    case, prefix+'/materials/'+receipt['receipt_id'], outside)
            finally:
                # 只有本测试恢复自己创建的链接，不经模型或产品接口自动修复。
                if leaf.is_symlink():
                    assert leaf.readlink() == moved
                    leaf.unlink()
                moved.rename(leaf)
            restored = await read_original(case.signed, prefix, doc, 'manager', case.raw)
            assert restored == original
            signed.executions.clear()
            final = await two_human_approvals(case, doc)
            assert [entry['status'] for entry in signed.executions] == [403, 202, 403, 202]
            assert [entry['signed']['origin'] for entry in signed.executions] == [
                'agent', 'human', 'agent', 'human']
            assert case.payloads == {} and case.downloads == ['pdf']
        verify = await readonly_verify(case)
    request.node.user_properties.append(('f13_process', json.dumps({
        'receipt': receipt, 'original': original, 'rejection': refusal, 'restored': restored,
        'final': final, 'confirmation_requests': signed.executions, 'source_removed': case.payloads == {},
        'download_count': len(case.downloads), 'readonly_verify': verify, 'processes': case.outcomes,
        'finance_port_closed': port_closed(case.config.port),
        'gateway_port_closed': port_closed(case.gateway_port)}, ensure_ascii=False)))

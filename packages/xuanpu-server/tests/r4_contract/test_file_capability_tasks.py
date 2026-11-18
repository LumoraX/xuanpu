"""文件型能力的受控任务适配（R4-B2）：完成契约只由已登记的能力修订决定。

文件型能力走办公成果校验（固定 CSV 输入、四类 table_summary 成果），不能凭只读回执判成功；
输入类型按交付描述核对；客户端不能自带执行方式或完成契约。内存与 PG 各跑一遍。
"""
from __future__ import annotations

import pytest
from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.tenancy import Principal
from xuanpu_server.work_tasks.models import InputSnapshot

from .test_capability_entrypoints import ENTRY, PACKAGE, _assigned

ALICE = Principal(role='tenant_member', tenant_id='acme', user_id='usr_alice', key_id=None)


def _input(index: int, mime: str = 'text/csv') -> InputSnapshot:
    """一份服务端固定的输入快照。"""
    suffix = 'csv' if mime == 'text/csv' else 'pdf'
    return InputSnapshot(item_id=f'ws_{index}', revision=1, sha256='a' * 64, size=10, mime_type=mime,
                         relative_name=f'input_{index}.{suffix}')


async def test_file_capability_uses_office_completion(r4_http, r4_world, r4_ids):
    """交付描述声明 office_files/table_summary：任务走办公成果校验，不是只读回执。"""
    await _assigned(r4_http, r4_world, r4_ids)
    platform = r4_world.platform
    decision = await platform.capabilities.admission.admit(ALICE, PACKAGE, ENTRY, None)
    profile = await platform.work_tasks.capability_profile('acme', decision, (_input(1), _input(2)))
    assert (profile.execution_kind, profile.completion_policy, profile.output_kind) == (
        'office', 'office_files', 'table_summary')
    unmanaged = await platform.work_tasks.capability_profile('acme', None, ())
    assert unmanaged.completion_policy == 'capability_receipt'


async def test_inputs_must_match_the_delivery_contract(r4_http, r4_world, r4_ids):
    """非 CSV、没有材料都拒绝（不落进会议纪要提取路径）；超过 8 份在输入结构上就不成立。"""
    await _assigned(r4_http, r4_world, r4_ids)
    platform = r4_world.platform
    decision = await platform.capabilities.admission.admit(ALICE, PACKAGE, ENTRY, None)
    for inputs in ((_input(1, 'application/pdf'),), (), (_input(1), _input(2, 'application/pdf'))):
        with pytest.raises(InvalidRequestError):
            await platform.work_tasks.capability_profile('acme', decision, inputs)


async def test_client_cannot_choose_completion_contract(r4_http, r4_world, r4_ids):
    """客户端在交办里夹带执行方式或完成契约：严格输入直接拒绝（该接口的校验错误为 400）。"""
    await _assigned(r4_http, r4_world, r4_ids)
    forged = await r4_http['alice'].post('/v1/work-tasks/from-capability', json={
        'request_id': 'forged', 'text': '汇总', 'capability': {'package': PACKAGE, 'entry': ENTRY},
        'completion_policy': 'capability_receipt'})
    assert forged.status_code == 400

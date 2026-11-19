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


@pytest.mark.parametrize(('entry', 'skill', 'script', 'accepted'), [
    ('review', 'review', 'summarize', True),          # 文件型能力：入口 skill 自己的汇总脚本
    (None, 'table-summary', 'summarize', True),       # 内置办公包：table-summary 子 skill
    ('review', 'review', 'inspect_headers', False),   # 同一 skill 的其他脚本不算
    ('review', 'helper', 'summarize', False),         # 包里别的 skill 的同名脚本不算
    (None, 'review', 'summarize', False),             # 未经能力入口受理的任务不认能力入口
])
async def test_summary_receipt_only_from_the_registered_entry_script(entry, skill, script, accepted):
    """成果摘要回执只认能力入口 skill（或内置 table-summary）的 summarize 完整 stdout。"""
    import json
    from pathlib import Path
    from types import SimpleNamespace

    import taifeng
    from xuanpu_server.work_tasks.runner import TaskRunner

    saved: list[dict] = []

    class Store:
        """只记录回执写入的任务存储替身。"""

        async def get(self, who, task_id):
            """当前任务版本。"""
            return SimpleNamespace(state_version=3)

        async def transition(self, who, task_id, status, **fields):
            """记下本次状态写入。"""
            saved.append(fields)

    runner = TaskRunner(Store(), None, None, None, None, SimpleNamespace(now=lambda: 1.0), None)
    task = SimpleNamespace(task_id='task1', output_kind='table_summary', entry=entry)
    invocation = taifeng.ScriptInvocation(descriptor=taifeng.ScriptDescriptor(
        skill_id=skill, name=script, language='python', path=Path('scripts') / (script + '.py'),
        timeout_seconds=30, max_output_bytes=65536, args_schema={}), args={}, cancel=taifeng.CancellationToken())
    hashes = {'outputs/manifest.json': 'a' * 64, 'outputs/summary.csv': 'b' * 64}
    result = taifeng.ScriptResult(exit_code=0, stdout=json.dumps({'outputs_sha256': hashes}), stderr='', duration_ms=1)
    await runner._script_receipt(ALICE, task, invocation, None)
    await runner._script_receipt(ALICE, task, invocation, result)
    if not accepted:
        assert saved == []
        return
    assert [entry['event_kind'] for entry in saved] == ['script_started', 'script_receipt_received']
    assert saved[0]['output_hashes'] == {} and saved[1]['output_hashes'] == hashes

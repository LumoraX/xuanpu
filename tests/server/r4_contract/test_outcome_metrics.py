"""能力结果统计（R4-F0）：总数不随分页变；各状态都列出、不从分母隐去；token 实际与上界分列、
没有记录的不当 0；没有价格表时费用为空不补 0；员工只看本人；管理员只看计数不看正文。内存与 PG。"""
from __future__ import annotations

import time

from server.core.tenancy import Principal
from server.domains.capabilities.bindings import InvocationBinding
from server.domains.work_tasks.execution_models import BudgetReservation
from server.domains.work_tasks.execution_store import initialize
from server.domains.work_tasks.models import WorkTaskRecord
from server.domains.work_tasks.store import save_record


def _who(user: str) -> Principal:
    """员工身份。"""
    return Principal(role='tenant_member', tenant_id='acme', user_id=user, key_id=None)


async def _seed(world, user: str, index: int, status: str, accepted_at: float,
                settled: int | None = None, held: int | None = None) -> None:
    """写一笔已绑定能力的任务（含可选的已结算/占用预留）。"""
    task_id = f'task_{user}_{index}'
    record = WorkTaskRecord(
        task_id=task_id, tenant_id='acme', user_id=user, actor='user:' + user, request_id=f'r{index}',
        input_fingerprint='a' * 64, text='汇总正文不应出现在统计里', timezone='Asia/Shanghai', inputs=(),
        package='office-review', package_version='1.0.0', package_digest='b' * 64, process_owner='p1',
        accepted_at=accepted_at, updated_at=accepted_at, status=status,
        finished_at=accepted_at + 1 if status in ('succeeded', 'failed', 'outcome_unknown') else None)
    async with world.platform.workspace.transaction(_who(user), 'work-task:budget:' + task_id) as state:
        record = initialize(state, record, _who(user))
        save_record(state, record, 'accepted')
        for kind, value in (('settled', settled), ('held', held)):
            if value is None:
                continue
            reservation = BudgetReservation(
                reservation_id=f'res_{task_id}_{kind}', task_id=task_id, grant_id=record.grant_id or '', attempt_no=1,
                day='2026-11-18', upper_bound=max(value, 1), state=kind,
                input_tokens=value if kind == 'settled' else 0, at=accepted_at)
            state.task_reservations[reservation.reservation_id] = reservation.model_dump(mode='json')
    await world.platform.store.binding_store.put(InvocationBinding(
        tenant_id='acme', kind='task', ref_id=task_id, user_id=user, user_epoch=1, capability_id='cap_x',
        capability_revision=1, package_name='office-review', package_version='1.0.0',
        package_digest='sha256:' + 'b' * 64, entry='review', installation_id='ins', installation_version=1,
        config_id='cfg', config_revision=1, config_digest='c' * 64, assignment_id='cas', assignment_epoch=1,
        release_id='crl', release_epoch=1, created_at=accepted_at))


async def _capability(world) -> None:
    """登记能力根，满足绑定表的外键（PG）。"""
    from server.domains.capabilities.delivery import Delivery
    from server.domains.capabilities.models import CapabilityRevision

    from .seed import delivery
    async with world.platform.organization.store.transaction('acme') as tx:
        tx.state.capabilities.revisions['cvr_x'] = CapabilityRevision(
            tenant_id='acme', id='cvr_x', created_at=1.0, capability_id='cap_x', capability_key='office-review',
            revision=1, digest='d' * 64, delivery=Delivery.model_validate(delivery('sha256:' + 'b' * 64)))


async def test_totals_statuses_tokens_and_cost(r4_http, r4_world):
    """总数独立于当前页；unknown/failed 都列出；实际与上界分列；无记录不当 0；费用为空。"""
    await _capability(r4_world)
    now = time.time()
    plan = [('succeeded', 100, None), ('succeeded', 50, None), ('failed', None, None),
            ('outcome_unknown', None, 400), ('running', None, None)]
    for index, (status, settled, held) in enumerate(plan):
        await _seed(r4_world, 'usr_alice', index, status, now - 100 + index, settled, held)
    body = (await r4_http['alice'].get('/v1/me/capability-outcomes', params={'limit': 2})).json()
    assert body['totals']['tasks'] == 5 and len(body['items']) == 2 and body['next_offset'] == 2
    assert body['totals']['succeeded'] == 2 and body['totals']['outcome_unknown'] == 1
    assert body['totals']['failed'] == 1 and body['totals']['running'] == 1
    assert body['technical_completion'] == {'succeeded': 2, 'cohort': 5, 'rate': 0.4}
    assert body['tokens'] == {'settled': 150, 'held_upper_bound': 400, 'tasks_with_records': 3,
                              'tasks_without_records': 2}
    assert body['cost'] == {'status': 'price_unavailable', 'amount': None, 'currency': None}
    assert body['cohort_basis'] == 'accepted_at' and body['aggregation_version'] == 1
    assert '汇总正文' not in str(body)


async def test_window_and_isolation(r4_http, r4_world):
    """按受理时间选队列；Bob 看不到 Alice 的任务；管理员按能力计数且不含任务明细。"""
    await _capability(r4_world)
    await _seed(r4_world, 'usr_alice', 1, 'succeeded', 1000.0, 10)
    await _seed(r4_world, 'usr_alice', 2, 'failed', 2000.0)
    response = await r4_http['alice'].get('/v1/me/capability-outcomes', params={'since': 1500, 'until': 3000})
    assert response.status_code == 200, response.text
    windowed = response.json()
    assert windowed['totals']['tasks'] == 1 and windowed['totals']['failed'] == 1
    assert (await r4_http['bob'].get('/v1/me/capability-outcomes')).json()['totals']['tasks'] == 0
    admin = (await r4_http['admin'].get('/v1/admin/capability-outcomes')).json()
    assert admin['totals']['tasks'] == 2
    assert [c['capability_id'] for c in admin['capabilities']] == ['cap_x']
    assert 'task_' not in str(admin) and '汇总正文' not in str(admin)
    assert (await r4_http['alice'].get('/v1/admin/capability-outcomes')).status_code == 403


async def test_acceptance_batch_and_observations(r4_http, r4_world):
    """批次样本须属于该能力修订；结论只追加、纠正须指明现行观察；通过率只除以有结论的样本。"""
    await _capability(r4_world)
    for index in range(4):
        await _seed(r4_world, 'usr_alice', index, 'succeeded', 1000.0 + index)
    admin = r4_http['admin']
    samples = [f'task_usr_alice_{i}' for i in range(4)]
    bad = await admin.post('/v1/admin/acceptance-batches', json={
        'request_id': 'b0', 'capability_id': 'cap_x', 'capability_revision': 1, 'name': '首批',
        'criteria': '金额与预期一致', 'samples': [*samples, 'task_other']})
    assert bad.status_code == 400
    created = await admin.post('/v1/admin/acceptance-batches', json={
        'request_id': 'b1', 'capability_id': 'cap_x', 'capability_revision': 1, 'name': '首批',
        'criteria': '金额与预期一致', 'samples': samples})
    assert created.status_code == 201, created.text
    batch = created.json()
    assert batch['counts'] == {'passed': 0, 'failed': 0, 'blocked': 0, 'not_checked': 4}
    assert batch['verification_rate'] is None
    path = f"/v1/acceptance-batches/{batch['id']}/observations"

    async def observe(request_id, task, verdict, supersedes=None):
        body = {'request_id': request_id, 'task_id': task, 'verdict': verdict, 'evidence_ref': 'evidence.json#' + task}
        if supersedes:
            body['supersedes'] = supersedes
        return await admin.post(path, json=body)

    first = await observe('o1', samples[0], 'failed')
    assert first.status_code == 201
    assert (await observe('o1', samples[0], 'failed')).json() == first.json()
    assert (await observe('o2', samples[0], 'passed')).status_code == 409
    assert (await observe('o3', samples[0], 'passed', first.json()['id'])).status_code == 201
    await observe('o4', samples[1], 'failed')
    await observe('o5', samples[2], 'blocked')
    assert (await observe('o6', 'task_other', 'passed')).status_code == 400
    detail = (await admin.get(f"/v1/acceptance-batches/{batch['id']}")).json()
    assert detail['counts'] == {'passed': 1, 'failed': 1, 'blocked': 1, 'not_checked': 1}
    assert detail['verification_rate'] == 0.5
    assert (await r4_http['alice'].get(f"/v1/acceptance-batches/{batch['id']}")).status_code == 403


async def test_csv_export_matches_page_and_hides_bodies(r4_http, r4_world):
    """导出与页面同口径：员工只导出本人、无记录 token 为空不填 0；管理员只有汇总；无正文；员工不能导管理员表。"""
    import csv
    import io

    await _capability(r4_world)
    await _seed(r4_world, 'usr_alice', 1, 'succeeded', 1000.0, 10)
    await _seed(r4_world, 'usr_alice', 2, 'failed', 1001.0)
    await _seed(r4_world, 'usr_bob', 3, 'succeeded', 1002.0, 5)
    mine = await r4_http['alice'].get('/v1/me/capability-outcomes/export')
    assert mine.status_code == 200 and mine.headers['content-type'].startswith('text/csv')
    assert mine.content.startswith(b'\xef\xbb\xbf')
    rows = list(csv.DictReader(io.StringIO(mine.content.decode('utf-8-sig'))))
    assert sorted(r['task_id'] for r in rows) == ['task_usr_alice_1', 'task_usr_alice_2']
    assert {r['task_id']: r['tokens_settled'] for r in rows} == {'task_usr_alice_1': '10', 'task_usr_alice_2': ''}
    assert '汇总正文' not in mine.text
    admin = await r4_http['admin'].get('/v1/admin/capability-outcomes/export')
    summary = list(csv.DictReader(io.StringIO(admin.content.decode('utf-8-sig'))))
    assert len(summary) == 1 and summary[0]['tasks'] == '3' and summary[0]['cost_amount'] == ''
    assert summary[0]['cost_status'] == 'price_unavailable' and 'task_' not in admin.text
    assert (await r4_http['alice'].get('/v1/admin/capability-outcomes/export')).status_code == 403


def test_export_neutralises_formula_prefixes():
    """以公式符号开头的文字前加单引号；数字与空值照常。"""
    from server.domains.outcomes.export import safe

    assert safe('=SUM(A1)') == "'=SUM(A1)" and safe('@cmd') == "'@cmd"
    assert safe(None) == '' and safe(12) == '12' and safe('cap_x') == 'cap_x'

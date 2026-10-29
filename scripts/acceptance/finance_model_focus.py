"""公开人工HTTP构造定点前置数据，局部模型通过不能冒充整条模型验收。"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from scripts.acceptance.finance_model_journey import (
    INVOICE_INFORMATION,
    REJECT_REASON,
    challenges,
    final_facts,
    manual,
    retained_original,
    upload,
)
from scripts.acceptance.finance_model_runtime import HarnessFailure


async def human_action(context: Any, actor: str, action: str, document: dict[str, Any],
                       reason: str | None = None) -> dict[str, Any]:
    """fixture人驱动器按当前队列、版本与计划执行，不伪造模型预检。"""
    doc = await manual(context, actor, 'getExpenseClaim', {'id': document['id']})
    parameters: dict[str, Any] = {}
    if action in {'approve', 'reject'}:
        queue = await manual(context, actor, 'listReviewQueue',
                             {'legal_entity_id': doc['legal_entity_id']})
        assigned = [item for item in queue['items'] if item['target_id'] == doc['id']]
        context.evidence.check('fixture_current_assignment', len(assigned) == 1 and
            assigned[0]['target_version'] == doc['version'], assigned)
        parameters = {'reason': reason, 'workflow_step_id': assigned[0]['workflow_step_id']}
    plan = await manual(context, actor, 'previewFinanceAction', {
        'Idempotency-Key': uuid4().hex, 'body': {'action': action,
        'target_type': 'expense_claim', 'target_id': doc['id'],
        'expected_version': doc['version'], 'parameters': parameters}}, True)
    current = await manual(context, actor, 'getActionPlan', {'id': plan['id']})
    context.evidence.check('fixture_human_plan_current', current['executable'] and
        current['digest'] == plan['digest'], {'actor': actor, 'action': action, 'plan': current})
    await manual(context, actor, 'executeFinanceAction', {'Idempotency-Key': uuid4().hex,
        'body': {'plan_id': current['id'], 'plan_digest': current['digest']}}, True)
    return await manual(context, 'owner', 'getExpenseClaim', {'id': doc['id']})


async def change_reason(context: Any, doc: dict[str, Any], reason: str) -> dict[str, Any]:
    """fixture只改当前单据事由，其余事实保持实际返回内容。"""
    current = await manual(context, 'owner', 'getExpenseClaim', {'id': doc['id']})
    return await manual(context, 'owner', 'updateExpenseClaim', {
        'id': current['id'], 'Idempotency-Key': uuid4().hex, 'body': {
            'expected_version': current['version'],
            'draft': {**current['content'], 'reason': reason}}}, True)


async def prepare_query_fixture(context: Any) -> tuple[str, bytes, dict[str, Any]]:
    """所有前置事实经真实公开human入口，不直写PG，不调用模型。"""
    if context.scenario != 'query-regression':
        raise HarnessFailure('FIXTURE_SCENARIO_REQUIRED')
    context.claim_source = 'human_fixture'
    receipt, raw, file = await upload(context)
    context.receipt = receipt
    doc = await manual(context, 'owner', 'createExpenseClaim', {'Idempotency-Key': uuid4().hex,
        'body': {'legal_entity_id': 'company-a', 'reason': '出差交通',
            'invoice_information': INVOICE_INFORMATION, 'evidence_ids': [receipt],
            'lines': [{'category_id': 'travel', 'amount': '128.50',
                       'occurred_on': '2026-10-22', 'reason': '出差交通'}]}}, True)
    doc = await change_reason(context, doc, '出差交通待补充')
    doc = await human_action(context, 'owner', 'submit_claim', doc)
    doc = await human_action(context, 'manager', 'reject', doc, REJECT_REASON)
    doc = await change_reason(context, doc, '拜访上海客户讨论项目需求')
    doc = await human_action(context, 'owner', 'submit_claim', doc)
    doc = await human_action(context, 'manager', 'approve', doc, '客户拜访事由已补充')
    await human_action(context, 'finance', 'approve', doc, '内部记录已复核')
    context.evidence.save('fixture-source.json', {'source': 'public_human_http',
        'model_created_or_reviewed_fixture': False, 'counts_as_complete_journey': False})
    return receipt, raw, file


async def query_regression(context: Any) -> None:
    """仅回查与两负向用真实模型；前置人审不计完整模型旅程。"""
    before = context.models.budget.local['attempts']
    receipt, raw, file = await prepare_query_fixture(context)
    context.evidence.check('fixture_zero_model_streams',
        context.models.budget.local['attempts'] == before, {'before': before,
        'after': context.models.budget.local['attempts']})
    doc = await final_facts(context)
    await retained_original(context, receipt, raw, file, doc)
    await challenges(context, doc)

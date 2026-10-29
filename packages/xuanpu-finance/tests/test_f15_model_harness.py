"""F15真实模型运行器的纯离线资源与授权回归。"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


def harness():
    """延迟导入，缺失实现时给出明确失败。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    return importlib.import_module('scripts.acceptance.finance_model_runtime')


def test_budget_survives_reopen(tmp_path: Path):
    """失败及新旅程仍累计同一全局用量。"""
    module = harness()
    path = tmp_path / 'budget.json'
    first = module.Budget(path, 'first')
    first.begin(30)
    first.finish({'input_tokens': 25, 'output_tokens': 12}, 2.0)
    second = module.Budget(path, 'second')
    assert second.data['attempts'] == 2  # 已完成探针也算一次。
    assert second.data['input_tokens'] == 46
    assert second.data['output_tokens'] == 23


def test_missing_usage_blocks_next_request(tmp_path: Path):
    """缺usage必须落盘并永久阻止后续自动消费。"""
    module = harness()
    budget = module.Budget(tmp_path / 'budget.json', 'one')
    budget.begin(10)
    with pytest.raises(module.HarnessFailure, match='USAGE_MISSING'):
        budget.finish(None, 1)
    reopened = module.Budget(tmp_path / 'budget.json', 'two')
    with pytest.raises(module.HarnessFailure, match='BUDGET_HALTED'):
        reopened.begin(10)


def test_event_limit_preserves_failure(tmp_path: Path):
    """原始事件超限也必须保存失败诊断。"""
    module = harness()
    budget = module.Budget(tmp_path / 'budget.json', 'one')
    budget.begin(10)
    with pytest.raises(module.HarnessFailure, match='EVENT_BYTES'):
        budget.events(128 * 1024 * 1024 + 1)
    assert json.loads((tmp_path / 'budget.json').read_text())['halted'] == 'EVENT_BYTES'


@pytest.mark.parametrize('target', ['executeFinanceAction', 'payInvoice', 'readReceiptOriginal',
                                     'api__finance__executeFinanceAction', 'shell'])
def test_model_confirmation_never_executes_business_decision(target: str):
    """精确白名单不允许确认执行、支付、原件或任意shell。"""
    assert not harness().allowed_approval(target)


def test_failed_result_cannot_turn_success(tmp_path: Path):
    """失败报告不能在finally写盘时变成成功。"""
    module = harness()
    report = module.Evidence(tmp_path, [])
    report.fail('MODEL_ERROR')
    report.complete()
    assert json.loads((tmp_path / 'result.json').read_text())['status'] == 'failed'


@pytest.mark.parametrize(('field', 'value', 'reason'), [
    ('attempts', 240, 'STREAM_LIMIT'), ('input_tokens', 3_000_000, 'INPUT_TOKENS'),
    ('output_tokens', 150_000 - 4095, 'OUTPUT_TOKENS'),
    ('request_bytes', 32 * 1024 * 1024, 'REQUEST_BYTES'), ('seconds', 5400, 'TIME_LIMIT'),
])
def test_global_hard_limit_stops_before_consumption(tmp_path: Path, field: str,
                                                    value: int, reason: str):
    """资源达到门槛必须在派发前失败，不能自动重置或扩额。"""
    module = harness()
    budget = module.Budget(tmp_path / 'budget.json', 'one')
    budget.data[field] = value
    previous = budget.data['attempts']
    with pytest.raises(module.HarnessFailure, match=reason):
        budget.begin(1)
    assert budget.data['attempts'] == previous


def test_model_phase_clock_includes_between_stream_tools(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch):
    """模型阶段墙钟包含工具间隙，不能仅累加网关stream时长。"""
    module = harness()
    ticks = iter([0.0, 1.0, 15.0])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks))
    budget = module.Budget(tmp_path / 'budget.json', 'one')
    budget.begin(1)
    budget.finish({'input_tokens': 1, 'output_tokens': 1}, 1.0)
    budget.begin(1)
    assert budget.local['seconds'] == 15.0
    assert budget.data['stream_seconds'] == 1.0


class OfflineSession:
    """离线协议夹具只转交固定事件，不调用任何网络。"""

    def __init__(self, usage: dict[str, int] | None):
        """保存测试事件，不装配真实网关。"""
        self.usage, self.request = usage, None

    async def stream(self, request):
        """记录收到的请求并产生公开协议事件。"""
        from taifeng import ResponseEvent

        self.request = request
        yield ResponseEvent(kind='completed', data={'usage': self.usage})


@pytest.mark.parametrize('requested', [128, 8192, None])
async def test_recorder_only_tightens_output_preserves_input_and_raw_events(tmp_path: Path,
                                                                         requested: int | None):
    """包装不得改变提示、工具或更小上限，真实事件及usage逐轮保存。"""
    from taifeng import ApiMessage, ApiRequest

    module = harness()
    evidence = module.Evidence(tmp_path / 'evidence', ['private-secret-value'])
    budget = module.Budget(tmp_path / 'budget.json', 'one')
    recorder = module.RecordingFactory(None, budget, evidence)
    inner = OfflineSession({'input_tokens': 10, 'output_tokens': 2})
    session = module.RecordingSession(inner, recorder)
    request = ApiRequest(model='balanced', system_prompt=['真实提示'],
        messages=[ApiMessage(role='user', content='private-secret-value')],
        max_output_tokens=requested)
    events = [event async for event in session.stream(request)]
    assert inner.request.model_dump(exclude={'max_output_tokens'}) == request.model_dump(
        exclude={'max_output_tokens'})
    assert inner.request.max_output_tokens == min(requested or 4096, 4096)
    trace = json.loads((tmp_path / 'evidence/model-002.json').read_text())
    assert trace['events'] == [event.model_dump(mode='json') for event in events]
    assert 'private-secret-value' not in (tmp_path / 'evidence/model-002.json').read_text()
    assert budget.data['input_tokens'] == 31


async def test_stream_missing_usage_persists_trace_and_halts(tmp_path: Path):
    """模型异常usage结束，原事件仍保存且后续请求不派发。"""
    from taifeng import ApiRequest

    module = harness()
    evidence = module.Evidence(tmp_path / 'evidence', [])
    budget = module.Budget(tmp_path / 'budget.json', 'one')
    recorder = module.RecordingFactory(None, budget, evidence)
    session = module.RecordingSession(OfflineSession(None), recorder)
    with pytest.raises(module.HarnessFailure, match='USAGE_MISSING'):
        _ = [event async for event in session.stream(ApiRequest(model='balanced'))]
    trace = json.loads((tmp_path / 'evidence/model-002.json').read_text())
    assert trace['events'][0]['kind'] == 'completed'
    assert json.loads((tmp_path / 'budget.json').read_text())['halted'] == 'USAGE_MISSING'


@pytest.mark.parametrize('action', ['execute_payment', 'submit_posting', 'verify_invoice'])
def test_preview_external_actions_never_get_confirmation(action: str):
    """预检工具白名单也不能用于支付、记账或查验动作。"""
    assert not harness().allowed_approval('api__finance__previewFinanceAction',
        {'body': {'action': action, 'target_type': 'expense_claim'}})


def checks():
    """只导入纯事实判定，不装配生产网关。"""
    harness()
    return importlib.import_module('scripts.acceptance.finance_model_checks')


def plan_trace(version: int = 3, call_id: str = 'preview', status: int = 201) -> dict:
    """建立合成的真实协议形状供观察层回归。"""
    arguments = {'body': {'target_id': 'claim', 'expected_version': version,
                          'target_type': 'expense_claim', 'action': 'reject'}}
    return {'request': {'messages': [{'role': 'tool', 'tool_call_id': call_id,
            'content': f'HTTP {status}\n{{"id":"plan","digest":"digest"}}'}]}, 'events': [
            {'kind': 'tool_call_done', 'data': {'name': 'api__finance__previewFinanceAction',
             'call_id': 'preview', 'arguments': json.dumps(arguments)}}]}


@pytest.mark.parametrize(('version', 'call_id'), [(2, 'preview'), (3, 'unrelated')])
def test_old_or_unrelated_plan_cannot_serve_as_model_preview(version: int, call_id: str):
    """同ID子串不够，必须当前版本和同一模型preview调用返回。"""
    module = checks()
    plan = {'id': 'plan', 'digest': 'digest', 'action': 'reject'}
    assert module.matching_model_plan([plan_trace(version, call_id)], plan,
                                      {'id': 'claim', 'version': 3}) == []
    assert module.matching_model_plan([plan_trace()], plan,
                                      {'id': 'claim', 'version': 3}) == ['preview']


@pytest.mark.parametrize('status', [200, 202, 204])
def test_model_preview_requires_formal_created_status(status: int):
    """预检正式契约是201，未声明200、仅受理或无正文不能证明收到行动计划。"""
    plan = {'id': 'plan', 'digest': 'digest', 'action': 'reject'}
    assert checks().matching_model_plan([plan_trace(status=status)], plan,
                                       {'id': 'claim', 'version': 3}) == []


@pytest.mark.parametrize('role', ['user', 'assistant'])
def test_model_preview_cannot_use_non_tool_claimed_plan(role: str):
    """用户或助手伪装的HTTP文本不能替代真实预检工具回包。"""
    trace = plan_trace()
    trace['request']['messages'][0]['role'] = role
    plan = {'id': 'plan', 'digest': 'digest', 'action': 'reject'}
    assert checks().matching_model_plan([trace], plan, {'id': 'claim', 'version': 3}) == []


def test_failed_tool_and_unrelated_success_do_not_count_as_reading():
    """函数提议、失败400及别的call成功都不能替代真实读取。"""
    module = checks()
    calls = [{'call_id': 'read', 'name': 'api__finance__getExpenseClaim'}]
    events = [{'kind': 'tool_call_completed', 'data': {
        'call_id': 'read', 'name': 'api__finance__getExpenseClaim',
        'is_error': True, 'output': 'HTTP 400\n{}'}},
        {'kind': 'tool_call_completed', 'data': {'call_id': 'other',
         'name': 'api__finance__getExpenseClaim', 'is_error': False, 'output': 'HTTP 200\n{}'}}]
    assert module.successful_tools(calls, [{'events': events}], [],
        phase='read:session', reader_id='owner', session_id='session') == set()


def tool_success_fixture(operation: str = 'createExpenseClaim', status: int = 201):
    """建立已恢复后无completed审计但模型真实收到HTTP返回的离线证据。"""
    name = 'api__finance__' + operation
    call = {'call_id': 'actual-call', 'name': name}
    trace = {'phase': 'create:session', 'actor_id': 'owner', 'session_id': 'session',
        'events': [{'kind': 'tool_call_done', 'data': call}], 'request': {
            'messages': [{'role': 'tool', 'tool_call_id': 'actual-call',
                          'content': f'HTTP {status}\n{{"id":"actual-document"}}'}],
            'input_items': []}}
    scope = {'phase': 'create:session', 'reader_id': 'owner', 'session_id': 'session'}
    return [call], trace, scope


@pytest.mark.parametrize(('operation', 'status', 'accepted'), [
    ('createExpenseClaim', 201, True), ('createExpenseClaim', 200, False),
    ('createExpenseClaim', 202, False), ('createExpenseClaim', 204, False),
    ('previewFinanceAction', 201, True), ('previewFinanceAction', 200, False),
    ('getExpenseClaim', 200, True), ('getExpenseClaim', 201, False),
    ('getExpenseClaim', 202, False), ('executeFinanceAction', 200, False),
])
def test_successful_tool_requires_exact_status_and_proposal(operation: str, status: int,
                                                           accepted: bool):
    """创建/预检须201、读取须200，无completed审计也必须有受控原始HTTP证据。"""
    calls, trace, scope = tool_success_fixture(operation, status)
    actual = checks().successful_tools(calls, [], [trace], **scope)
    assert actual == ({calls[0]['name']} if accepted else set())


@pytest.mark.parametrize('invalid', ['missing_raw', 'call_id', 'proposal_id', 'proposal_name',
    'phase', 'actor', 'session', 'user', 'assistant', 'raw_error', 'output_name', 'not_http'])
def test_successful_tool_raw_requires_actual_provenance(invalid: str):
    """缺返回或错调用/名称/场景/人员/会话、伪文字、显式错误均不能充当执行事实。"""
    calls, trace, scope = tool_success_fixture()
    message = trace['request']['messages'][0]
    if invalid == 'missing_raw':
        trace['request']['messages'] = []
    elif invalid == 'call_id':
        message['tool_call_id'] = 'unrelated'
    elif invalid == 'proposal_id':
        trace['events'] = []
    elif invalid == 'proposal_name':
        trace['events'][0]['data'] = {**calls[0], 'name': 'api__finance__getExpenseClaim'}
    elif invalid in ('phase', 'actor', 'session'):
        trace[{'phase': 'phase', 'actor': 'actor_id', 'session': 'session_id'}[invalid]] = 'other'
    elif invalid in ('user', 'assistant'):
        message['role'] = invalid
    elif invalid == 'raw_error':
        message['is_error'] = True
    elif invalid == 'output_name':
        message['name'] = 'api__finance__getExpenseClaim'
    else:
        message['content'] = '{"status":201}'
    assert checks().successful_tools(calls, [], [trace], **scope) == set()


@pytest.mark.parametrize(('status', 'name', 'error', 'accepted'), [
    (201, 'createExpenseClaim', False, True), (202, 'createExpenseClaim', False, False),
    (201, 'getExpenseClaim', False, False), (201, 'createExpenseClaim', True, False),
    (201, 'createExpenseClaim', None, False),
])
def test_explicit_failed_audit_cannot_be_masked_by_raw(status: int, name: str,
                                                     error: bool | None, accepted: bool):
    """已有审计明确失败、状态或名称不一致时，原始正例不能覆盖矛盾证据。"""
    calls, trace, scope = tool_success_fixture()
    segments = [{'events': [{'kind': 'tool_call_completed', 'data': {
        'call_id': 'actual-call', 'name': 'api__finance__' + name, 'is_error': error,
        'output': f'HTTP {status}\n{{}}'}}]}]
    assert checks().successful_tools(calls, segments, [trace], **scope) == (
        {calls[0]['name']} if accepted else set())


@pytest.mark.parametrize('wrong_scope', ['phase', 'actor_id', 'session_id'])
def test_tool_return_scope_cannot_borrow_another_trace(wrong_scope: str):
    """提议与回包分属两轮时，不能串用另一个场景、人员或会话的HTTP正例。"""
    from copy import deepcopy

    calls, proposal, scope = tool_success_fixture()
    response = deepcopy(proposal)
    proposal['request']['messages'] = []
    response['events'] = []
    assert checks().successful_tools(calls, [], [proposal, response], **scope) == {calls[0]['name']}
    response[wrong_scope] = 'other'
    assert checks().successful_tools(calls, [], [proposal, response], **scope) == set()


def test_function_call_output_is_actual_raw_tool_result():
    """Responses input_items的真实function_call_output与role=tool拥有相同证据要求。"""
    calls, trace, scope = tool_success_fixture()
    trace['request']['messages'] = []
    trace['request']['input_items'] = [{'type': 'function_call_output',
        'call_id': 'actual-call', 'output': 'HTTP 201\n{"id":"actual-document"}'}]
    assert checks().successful_tools(calls, [], [trace], **scope) == {calls[0]['name']}
    trace['request']['input_items'][0]['type'] = 'message'
    assert checks().successful_tools(calls, [], [trace], **scope) == set()


@pytest.mark.parametrize(('container', 'role', 'kind'), [
    ('messages', 'user', 'function_call_output'),
    ('input_items', 'tool', 'message'),
    ('messages', 'tool', 'function_call_output'),
    ('input_items', 'assistant', 'function_call_output'),
])
def test_raw_tool_origin_cannot_cross_container_boundaries(container: str, role: str, kind: str):
    """按原始容器识别工具回包，不能用另一容器的role/type条件洗白伪文本。"""
    calls, trace, scope = tool_success_fixture()
    trace['request'] = {'messages': [], 'input_items': []}
    trace['request'][container] = [{'role': role, 'type': kind,
        'tool_call_id': 'actual-call', 'call_id': 'actual-call',
        'content': 'HTTP 201\n{}', 'output': 'HTTP 201\n{}'}]
    assert checks().successful_tools(calls, [], [trace], **scope) == set()


@pytest.mark.parametrize(('container', 'role', 'kind'), [
    ('messages', 'user', 'function_call_output'),
    ('input_items', 'tool', 'message'),
    ('messages', 'tool', 'function_call_output'),
    ('input_items', 'assistant', 'function_call_output'),
])
def test_model_preview_raw_origin_cannot_cross_containers(container: str, role: str, kind: str):
    """真实计划ID和摘要也不能让跨容器伪装的消息变成预检工具返回。"""
    trace = plan_trace()
    trace['request'] = {'messages': [], 'input_items': []}
    output = 'HTTP 201\n{"id":"plan","digest":"digest"}'
    trace['request'][container] = [{'role': role, 'type': kind,
        'tool_call_id': 'preview', 'call_id': 'preview', 'content': output, 'output': output}]
    plan = {'id': 'plan', 'digest': 'digest', 'action': 'reject'}
    assert checks().matching_model_plan([trace], plan, {'id': 'claim', 'version': 3}) == []


@pytest.mark.parametrize(('operation', 'status'), [
    ('createExpenseClaim', '201'), ('listSupportingMaterialImports', '200'),
    ('getSupportingMaterialImport', '200'), ('listReviewQueue', '200'),
    ('getExpenseClaim', '200'), ('listExpenseClaimHistory', '200'),
    ('updateExpenseClaim', '200'), ('previewFinanceAction', '201'),
    ('listExpenseClaimSummaries', '200'),
])
def test_journey_success_status_is_declared_by_actual_openapi(operation: str, status: str):
    """旅程九个目标逐条对照发布契约，不把任意2xx或202当成已完成。"""
    from .test_package import contracts, operations

    contract = operations(contracts()['finance'])[operation][2]
    assert set(contract['responses']) - {'default'} == {status}
    calls, trace, scope = tool_success_fixture(operation, int(status))
    assert checks().successful_tools(calls, [], [trace], **scope) == {calls[0]['name']}


def test_child_correct_answer_cannot_mask_wrong_root_reply():
    """根答复仍说draft时，子Skill曾说approved不能通过语义验收。"""
    module = checks()
    traces = [{'session_id': 's', 'request': {'system_prompt': [
        '<entry_skill id="finance-assistant" name="finance-assistant">']},
        'events': [{'kind': 'text_delta', 'data': {'text': 'draft'}},
                   {'kind': 'completed', 'data': {'end_turn': True}}]},
        {'session_id': 's', 'request': {'system_prompt': ['<entry_skill id="finance-review">']},
         'events': [{'kind': 'text_delta', 'data': {'text': 'approved'}},
                    {'kind': 'completed', 'data': {'end_turn': True}}]}]
    assert module.root_reply(traces) == ('draft', 's')


def test_tool_input_leak_is_found_even_when_output_refuses(tmp_path: Path):
    """越权工具结果泄漏必须失败，不能因最终自然语言拒绝而遮掩。"""
    from types import SimpleNamespace

    module = checks()
    evidence = harness().Evidence(tmp_path, [])
    evidence.save('model-002.json', {'phase': 'unsafe:session', 'actor_id': 'outsider',
        'request': {'messages': [{'role': 'tool', 'content': 'private-receipt-id'}]},
        'events': [{'kind': 'text_delta', 'data': {'text': '无权查看'}}]})
    leaked = module.leaked_files(SimpleNamespace(evidence=evidence), 'unsafe', 'outsider',
                                 ['private-receipt-id'])
    assert leaked == ['raw_model:private-receipt-id']


async def test_existing_evidence_refused_before_state_or_model(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch):
    """新旅程不能覆盖历史证据，失败也不创建下一轮状态目录。"""
    from types import SimpleNamespace

    harness()
    module = importlib.import_module('scripts.acceptance.finance_model')
    old = tmp_path / 'old-evidence'
    old.mkdir()
    (old / 'result.json').write_text('retained failure')
    monkeypatch.setattr(module, 'private_text', lambda path: 'postgresql://u:p@127.0.0.1:5432/finance_test')
    monkeypatch.setattr(module, 'model_config', lambda path: {'api_key': 'not-a-real-key'})
    options = SimpleNamespace(dsn_file=tmp_path / 'dsn', model_config=tmp_path / 'models',
                              evidence=old, state=tmp_path / 'new-state')
    with pytest.raises(ValueError, match='FRESH_EVIDENCE_DIRECTORY_REQUIRED'):
        await module.run(options)
    assert not options.state.exists()
    assert (old / 'result.json').read_text() == 'retained failure'


@pytest.mark.parametrize('reply', [
    '付款128.50，入账0.00', '付款0.00，记账128.50',
    '未付款，已付款128.50；未入账', '付款0.00，但已经支付；入账0.00',
    '付款0.00', '入账0.00', '报销金额128.50，0.00',
])
def test_payment_and_posting_reply_rejects_nonzero_contradictory_or_missing(reply: str):
    """每个维度分别判定，另一维零不能遮盖非零、矛盾或缺失。"""
    assert not checks().financial_reply_is_zero(reply)


@pytest.mark.parametrize('reply', [
    '已批准128.50元。付款金额：0.00；入账金额：0.00。',
    '报销128.50元，尚未付款，也未入账。',
    '**付款**：未执行\n**记账**：未执行',
])
def test_payment_and_posting_reply_accepts_independent_explicit_zero(reply: str):
    """普通根回复对两维都明确零或尚未执行才通过。"""
    assert checks().financial_reply_is_zero(reply)


def test_actual_query_zero_amount_labels_are_not_execution_claims():
    """真实模型金额字段为零，不应把字段名本身误判为已执行付款。"""
    path = (Path(__file__).resolve().parents[3] /
        'docs/guides/releases/evidence/finance-f15-execution-2026-10-29/'
        'query-regression-fixed/result.json')
    result = json.loads(path.read_text())
    reply = next(c['actual'] for c in result['checks'] if c['name'] == 'fresh_query_semantic_facts')
    assert checks().financial_reply_is_zero(reply)


@pytest.mark.parametrize('reply', [
    '已付款：0.00 元；已记账：0.00 元',
    '**已支付金额**：**0.00元**；已入账金额：0元',
    '已付款金额：0.00 CNY；已记账：0.00元',
    '已入账：0；未付款',
])
def test_explicit_zero_amount_field_labels_are_not_action_statements(reply: str):
    """仅明确金额标签的零值可接受，仍要求两个独立维度。"""
    assert checks().financial_reply_is_zero(reply)


@pytest.mark.parametrize('reply', [
    '已付款：0.01元；未入账', '未付款；已记账金额：1元',
    '已付款成功：0.00元；未入账', '已经支付；未入账',
    '已付款：0.00元 已执行；未入账',
    '已付款：0.00元；已支付128.50元；未入账',
    '已付款：0.00元；已记账：2元',
    '已付款：0.00元；已记账：0.00元；支付成功',
    '已付款：0.00元；已记账：0.00元；已支付',
    '已经实际付款：0.00元；未入账',
    '付款金额：-1元；付款金额：0.00元；未入账',
    '实际已付款：0.00元；未记账',
    '本次已付款：0.00元；未入账',
    '操作已支付：0.00元；未记账',
    '实际       已付款：0.00元；未记账',
    '已付款：0元；已记账：0元；已执行付款',
    '已付款：0元；已记账：0元；付款已经完成',
    '已付款：0元；已记账：0元；已执行记账',
    '已付款：0元；已记账：0元；记账已经完成',
    '已付款：0元；已记账：0元；已经执行付款',
    '已付款：0元；已记账：0元；记账已经执行',
])
def test_zero_field_cannot_mask_nonzero_or_execution_statement(reply: str):
    """零标签不取消同句或后续成功声明，另一维也不能掩盖非零。"""
    assert not checks().financial_reply_is_zero(reply)


@pytest.mark.parametrize('prefix', ['已', '已经'])
@pytest.mark.parametrize('actual', ['', '实际'])
@pytest.mark.parametrize('verb', ['执行', '完成'])
@pytest.mark.parametrize('position', ['before', 'after'])
@pytest.mark.parametrize('alias', ['付款', '支付', '记账', '入账'])
def test_zero_fields_never_hide_explicit_execution_word_matrix(
    prefix: str, actual: str, verb: str, position: str, alias: str,
):
    """完整固定词形矩阵覆盖两维前后置动作，不靠零金额取消后续执行。"""
    action = prefix + actual + verb
    statement = action + alias if position == 'before' else alias + action
    assert not checks().financial_reply_is_zero('已付款：0元；已记账：0元；' + statement)


def test_public_history_same_count_with_tampered_content_fails():
    """数量、ID都正确但内容被改写的历史不能通过。"""
    import copy

    module = checks()
    revisions = [{'version': 1, 'content': {'reason': '原事由'}}]
    decisions = [{'id': 'd', 'actor_id': 'manager', 'action': 'reject', 'workflow_id': 'w',
                  'step_id': 's', 'data': {'origin': 'human', 'parameters': {'reason': '补充'}}}]
    items = [{'id': 'revision:claim:1', 'kind': 'revision', 'data': revisions[0]},
             {'id': 'decision:d', 'kind': 'decision', 'data': {**decisions[0]['data'],
              'actor_id': 'manager', 'action': 'reject', 'workflow_id': 'w', 'step_id': 's'}}]
    assert module.public_history_matches(items, 'claim', revisions, decisions)
    bad = copy.deepcopy(items)
    bad[1]['data']['parameters']['reason'] = '被伪造的批准'
    assert not module.public_history_matches(bad, 'claim', revisions, decisions)


@pytest.mark.parametrize('reply', ['未付款但已付款；未入账', '未付款但已付款0.00；未入账'])
def test_prior_negation_cannot_cancel_later_positive_execution(reply: str):
    """前半句的未付款不能否定后半句已付款，零金额也不等于未执行。"""
    assert not checks().financial_reply_is_zero(reply)


def history_read_fixture():
    """模拟真实审计显示截断，完整HTTP回包只在模型实际输入中。"""
    name = 'api__finance__listExpenseClaimHistory'
    page = {'items': [{'kind': 'decision', 'data': {'actor_id': 'manager',
            'parameters': {'reason': '补充客户事由'}}}], 'next_cursor': None}
    trace = {'phase': 'resubmit:session', 'actor_id': 'owner', 'events': [
        {'kind': 'tool_call_done', 'data': {'call_id': 'history', 'name': name,
         'arguments': json.dumps({'id': 'claim', 'limit': 1, 'cursor': None})}}],
        'request': {'messages': [{'role': 'tool', 'tool_call_id': 'history',
                    'content': 'HTTP 200\n' + json.dumps(page)}], 'input_items': []}}
    segments = [{'events': [{'kind': 'tool_call_completed', 'data': {
        'call_id': 'history', 'name': name, 'is_error': False,
        'output': 'HTTP 200\n{"items": [已省略545字符]}'}}]}]
    return trace, segments


def test_history_reads_complete_raw_not_truncated_audit():
    """真实完整tool input证明读取，截断SSE只用于确认调用成功。"""
    trace, segments = history_read_fixture()
    assert checks().history_decision_read([trace], segments, phase='resubmit:session',
        reader_id='owner', document_id='claim', reason='补充客户事由', decision_actor_id='manager')


@pytest.mark.parametrize('missing', ['raw', 'call_id', 'reader', 'claim', 'phase', 'tool', 'decision'])
def test_history_read_requires_exact_raw_provenance(missing: str):
    """缺完整输入、错调用/读取身份/目标/场景/工具/决定身份均不得补证。"""
    trace, segments = history_read_fixture()
    if missing == 'raw':
        trace['request']['messages'] = []
    elif missing == 'call_id':
        trace['request']['messages'][0]['tool_call_id'] = 'other'
    elif missing == 'reader':
        trace['actor_id'] = 'manager'
    elif missing == 'claim':
        trace['events'][0]['data']['arguments'] = json.dumps({'id': 'other', 'limit': 1})
    elif missing == 'phase':
        trace['phase'] = 'earlier:session'
    elif missing == 'tool':
        trace['events'][0]['data']['name'] = 'api__finance__getExpenseClaim'
    else:
        trace['request']['messages'][0]['content'] = trace['request']['messages'][0][
            'content'].replace('manager', 'outsider')
    assert not checks().history_decision_read([trace], segments, phase='resubmit:session',
        reader_id='owner', document_id='claim', reason='补充客户事由', decision_actor_id='manager')

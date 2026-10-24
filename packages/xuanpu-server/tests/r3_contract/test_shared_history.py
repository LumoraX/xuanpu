"""共享主对话的实际模型请求、缓存、历史和后台扫描保持来源边界。"""
from __future__ import annotations

import json

from xuanpu_server.tenancy import Principal

from .test_shared_context import context_for, invite


async def setup_context(shared_http, shared_id):
    """授予Bob只读并原生选择固定来源。"""
    grant = (await invite(shared_http, shared_id, 'history-reader', 1)).json()
    context = (await context_for(shared_http['bob'], shared_id, 'history-source', 2)).json()
    return grant, context


async def revoke(shared_http, shared_id, grant):
    """撤销原始授权，不依赖模拟的HTTP拒绝。"""
    result = await shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/members/' + grant['id'] + '/revoke', json={
        'request_id': 'history-revoke', 'confirmed': True, 'expected_version': 2})
    assert result.status_code == 200, result.text


async def test_shared_route_history_replay_and_personal_model_isolation(shared_http, shared_id, r3_world, step):
    """共享正文进入模型有来源；普通对话不带入；撤权后旧结果及历史不泄露。"""
    grant, context = await setup_context(shared_http, shared_id)
    body = {'text': '概括已选资料', 'request_id': 'shared-chat', 'shared_context_id': context['id']}
    r3_world.models.script(step(text=json.dumps({'message': '共享摘要机密'})))
    response = await shared_http['bob'].post('/v1/assistant/route', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['message'] == '共享摘要机密'
    assert '仅共享事项正文' in r3_world.models.last_prompt()
    again = await shared_http['bob'].post('/v1/assistant/route', json=body)
    assert again.status_code == 200 and len(r3_world.models.requests) == 1
    history = (await shared_http['bob'].get('/v1/workspace/messages')).json()['items']
    assert all(row['sources'] and row['source_context_id'] == context['id'] for row in history)
    r3_world.models.script(step(text=json.dumps({'package': None, 'entry': None, 'message': '普通回答'})))
    ordinary = await shared_http['bob'].post('/v1/assistant/route', json={'text': '你好', 'request_id': 'personal-chat'})
    assert ordinary.status_code == 200, ordinary.text
    assert '共享摘要机密' not in r3_world.models.last_prompt()
    assert '仅共享事项正文' not in r3_world.models.last_prompt()
    await revoke(shared_http, shared_id, grant)
    before = len(r3_world.models.requests)
    denied = await shared_http['bob'].post('/v1/assistant/route', json=body)
    assert denied.status_code == 403, denied.text
    history = (await shared_http['bob'].get('/v1/workspace/messages')).json()['items']
    assert '共享摘要机密' not in json.dumps(history, ensure_ascii=False)
    assert any(row.get('source_blocked') is True for row in history)
    assert len(r3_world.models.requests) == before


async def test_shared_route_cannot_make_personal_business_proposals(shared_http, shared_id, r3_world, step):
    """共享模型只输出文本，不能把内容保存成无来源个人待办、执行定义或文件。"""
    _, context = await setup_context(shared_http, shared_id)
    r3_world.models.script(step(text=json.dumps({'message': '摘要', 'workspace_draft': {'kind': 'todo', 'data': {'title': '泄露'}}})))
    response = await shared_http['bob'].post('/v1/assistant/route', json={
        'text': '创建待办', 'request_id': 'bad-shared-proposal', 'shared_context_id': context['id']})
    assert response.status_code == 503, response.text
    items = (await shared_http['bob'].get('/v1/assistant/proposals')).json()['items']
    assert items == []


async def test_revocation_after_sampling_does_not_save_or_return_output(shared_http, shared_id, r3_world, step, monkeypatch):
    """模型已收到内容后无法追回，撤权发生在保存前时必须拒绝新输出。"""
    grant, context = await setup_context(shared_http, shared_id)
    assistant = r3_world.platform.assistant
    original = assistant._sample
    async def sample(*args, **kwargs):
        """在模型完成后、持久结果前插入真实撤权。"""
        result = await original(*args, **kwargs)
        await revoke(shared_http, shared_id, grant)
        return result
    monkeypatch.setattr(assistant, '_sample', sample)
    r3_world.models.script(step(text=json.dumps({'message': '不应保存的共享输出'})))
    response = await shared_http['bob'].post('/v1/assistant/route', json={
        'text': '概括', 'request_id': 'revoke-during-route', 'shared_context_id': context['id']})
    assert response.status_code == 403, response.text
    raw = await r3_world.platform.workspace.store.messages('acme', 'usr_bob', 0, 100)
    assert '不应保存的共享输出' not in json.dumps(raw, ensure_ascii=False)


async def test_proactive_never_uses_source_messages_as_private_evidence(shared_http, shared_id, r3_world, step):
    """即使当前有权限，主动扫描也不能把共享承诺写入无来源的私人跟进。"""
    _, context = await setup_context(shared_http, shared_id)
    r3_world.models.script(step(text=json.dumps({'message': '共享摘要机密'})))
    response = await shared_http['bob'].post('/v1/assistant/route', json={
        'text': '我承诺明天处理共享事项', 'request_id': 'proactive-shared', 'shared_context_id': context['id']})
    assert response.status_code == 200, response.text
    platform = r3_world.platform
    who = Principal(role='tenant_member', tenant_id='acme', user_id='usr_bob', key_id=None)
    # 直接走后台读取路径，不能仅在HTTP消息入口过滤。
    messages = await platform.workspace.source_history.personal_messages('acme', 'usr_bob', 0, 100)
    assert messages == []
    assert await platform.workspace.pending_proposals(who) == []
    before = len(r3_world.models.requests)
    assert await platform.proactive.evaluate_owner('acme', 'usr_bob') is True
    assert len(r3_world.models.requests) == before
    async with platform.workspace.transaction(who) as state:
        assert state.scan_state['message_cursor'] == 2
        assert not state.followups

"""普通对话只提共享草稿，明确来源对话才读取选定固定材料。"""
from __future__ import annotations

import json


async def test_ordinary_assistant_suggests_shared_without_writing(shared_http, r3_world, step):
    """无部门仍可提出共享事项，模型不能指定员工权限或自行保存。"""
    r3_world.models.script(step(text=json.dumps({'package': '@shared', 'entry': 'create',
        'message': None, 'shared_draft': {'title': '与同事核对', 'content': '待确认的共同工作'}})))
    body = {'request_id': 'shared-native-suggestion', 'text': '新建一个与同事共同核对的共享事项'}
    response = await shared_http['alice'].post('/v1/assistant/route', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['shared_draft']['title'] == '与同事核对'
    assert (await shared_http['alice'].get('/v1/shared-matters')).json()['total'] == 0
    again = await shared_http['alice'].post('/v1/assistant/route', json=body)
    assert again.json() == response.json() and len(r3_world.models.requests) == 1


async def test_selected_text_material_reaches_shared_conversation_only(shared_http, shared_id, r3_world, step):
    """实际模型请求包含已选材料正文，不把它混入后续普通个人对话。"""
    alice = shared_http['alice']
    path = '/v1/shared-matters/' + shared_id
    pub = await alice.post(path + '/material-uploads', params={'request_id': 'chat-text-file',
        'expected_version': 1, 'name': '核对.txt', 'confirmed': 'true'}, content='固定材料里的独特金额9876'.encode(),
        headers={'Content-Type': 'text/plain'})
    assert pub.status_code == 202, pub.text
    materials = (await alice.get(path + '/materials')).json()['items']
    revision = materials[0]['revisions'][0]['id']
    current = (await alice.get(path)).json()
    context = await alice.post('/v1/shared-contexts', json={'request_id': 'chat-select-text', 'confirmed': True,
        'expected_version': current['version'], 'shared_id': shared_id, 'kind': 'assistant_conversation',
        'material_revision_ids': [revision]})
    assert context.status_code == 201, context.text
    r3_world.models.script(step(text=json.dumps({'message': '根据选定材料核对金额9876'})))
    response = await alice.post('/v1/assistant/route', json={'request_id': 'chat-read-text',
        'shared_context_id': context.json()['id'], 'text': '核对资料中的金额'})
    assert response.status_code == 200, response.text
    captured = r3_world.models.requests[-1].model_dump_json()
    assert '固定材料里的独特金额9876' in captured
    r3_world.models.script(step(text=json.dumps({'package': None, 'entry': None, 'message': '普通答复'})))
    assert (await alice.post('/v1/assistant/route', json={'text': '你好', 'request_id': 'chat-after-source'})).status_code == 200
    assert '独特金额9876' not in r3_world.models.requests[-1].model_dump_json()

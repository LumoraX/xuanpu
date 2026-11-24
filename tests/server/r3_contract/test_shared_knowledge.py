"""共享事项材料作为知识来源（R4-D0 扩展）：只有此刻有权读的成员能登记与检索；撤销成员、撤回材料后
来源失效，检索与问答都不再出现原文；登记不复制分享权。内存与 PG。"""
from __future__ import annotations

from server.domains.knowledge.service import KnowledgeConfig

SECRET = '共享口径-丁五号'


async def _material(alice, shared_id: str) -> str:
    """Alice 把本人一份文本发布进共享事项，返回材料标识。"""
    uploaded = await alice.post('/v1/workspace/files', params={'name': '共享口径.txt', 'request_id': 'kn-src'},
                                content=f'本季度{SECRET}：按合同签订日确认。'.encode(), headers={'Content-Type': 'text/plain'})
    assert uploaded.status_code == 200, uploaded.text
    path = '/v1/shared-matters/' + shared_id
    created = await alice.post(path + '/materials', json={
        'request_id': 'kn-publish', 'confirmed': True, 'expected_version': 1,
        'source_item_id': uploaded.json()['id'], 'source_revision': 1})
    assert created.status_code == 202, created.text
    publication = (await alice.get(path + '/publications/' + created.json()['id'])).json()
    assert publication['status'] == 'ready', publication
    return str(publication['material_id'])


async def _register(client, shared_id: str, material: str):
    """登记共享材料第 1 版为知识来源。"""
    return await client.post('/v1/me/knowledge/sources', json={
        'kind': 'shared_material', 'matter_id': shared_id, 'material_id': material, 'revision': 1})


async def _search(client):
    """检索秘密标记。"""
    response = await client.post('/v1/me/knowledge/search', json={'query': SECRET})
    assert response.status_code == 200, response.text
    return response.json()


async def test_member_registers_and_loses_it_after_revocation(shared_http, shared_id, r3_world, step):
    """成员可登记与检索；非成员 404；撤销成员后检索为空、来源 stale、问答失效且不再调模型。"""
    r3_world.platform.knowledge.config = KnowledgeConfig(enabled=True, tenants=('acme',))
    alice, bob = shared_http['alice'], shared_http['bob']
    material = await _material(alice, shared_id)
    assert (await _register(bob, shared_id, material)).status_code == 404
    grant = await alice.post('/v1/shared-matters/' + shared_id + '/members', json={
        'request_id': 'kn-member', 'confirmed': True, 'expected_version': 3,
        'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer'})
    assert grant.status_code == 201, grant.text
    registered = await _register(bob, shared_id, material)
    assert registered.status_code == 201, registered.text
    source = registered.json()
    assert source['kind'] == 'shared_material' and source['matter_id'] == shared_id and source['item_id'] is None
    hits = await _search(bob)
    assert hits['total'] == 1 and SECRET in hits['hits'][0]['excerpt']
    context = (await bob.post('/v1/me/knowledge/contexts', json={
        'request_id': 'kn-ctx', 'pins': [{'source_id': source['id'], 'ordinal': 0}]})).json()
    r3_world.models.script(step(text='按合同签订日 [1]'))
    assert (await bob.post(f"/v1/me/knowledge/contexts/{context['id']}/ask",
                           json={'request_id': 'kn-q1', 'question': '口径？'})).status_code == 200
    revoked = await alice.post('/v1/shared-matters/' + shared_id + '/members/' + grant.json()['id'] + '/revoke',
                               json={'request_id': 'kn-revoke', 'confirmed': True, 'expected_version': 4})
    assert revoked.status_code == 200, revoked.text
    assert await _search(bob) == {'hits': [], 'total': 0}
    states = {s['id']: s['status'] for s in (await bob.get('/v1/me/knowledge/sources')).json()['sources']}
    assert states[source['id']] == 'stale'
    before = len(r3_world.models.requests)
    response = await bob.post(f"/v1/me/knowledge/contexts/{context['id']}/ask",
                              json={'request_id': 'kn-q2', 'question': '口径？'})
    assert response.status_code == 403 and SECRET not in response.text
    assert len(r3_world.models.requests) == before


async def test_withdrawn_material_stops_appearing_and_cannot_refresh(shared_http, shared_id, r3_world):
    """材料撤回后连发布者本人的来源也失效；共享来源不提供自动换版。"""
    r3_world.platform.knowledge.config = KnowledgeConfig(enabled=True, tenants=('acme',))
    alice = shared_http['alice']
    material = await _material(alice, shared_id)
    source = (await _register(alice, shared_id, material)).json()
    refresh = await alice.post(f"/v1/me/knowledge/sources/{source['id']}/refresh",
                               json={'expected_version': source['version']})
    assert refresh.status_code == 409
    assert (await _search(alice))['total'] == 1
    withdrawn = await alice.post('/v1/shared-matters/' + shared_id + '/materials/' + material + '/withdraw',
                                 json={'request_id': 'kn-withdraw', 'confirmed': True, 'expected_version': 3})
    assert withdrawn.status_code == 200, withdrawn.text
    assert (await _search(alice))['total'] == 0

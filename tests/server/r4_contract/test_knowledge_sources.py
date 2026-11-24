"""本人知识来源与检索（R4-D0）：只登记本人文件的明确版本；检索只在此刻可读的来源里进行，
无权来源的正文、命中数都不出现；格式与编码实际核对；字面匹配；有更新只提示不自动换。内存与 PG。"""
from __future__ import annotations

import pytest

from server.domains.knowledge.service import KnowledgeConfig

SECRET = '机密标记-甲七号'


@pytest.fixture
def enabled(r4_world):
    """租户开启知识检索。"""
    r4_world.platform.knowledge.config = KnowledgeConfig(enabled=True, tenants=('acme',))


async def _upload(client, name: str, content: bytes, mime: str = 'text/plain', request_id: str | None = None):
    """上传本人一份文件，返回文件记录。"""
    response = await client.post('/v1/workspace/files', params={'name': name, 'request_id': request_id or name},
                                 content=content, headers={'Content-Type': mime})
    assert response.status_code == 200, response.text
    return response.json()


async def _register(client, item_id: str, revision: int = 1):
    """登记来源。"""
    return await client.post('/v1/me/knowledge/sources', json={'item_id': item_id, 'revision': revision})


async def _search(client, query: str):
    """检索。"""
    response = await client.post('/v1/me/knowledge/search', json={'query': query})
    assert response.status_code == 200, response.text
    return response.json()


async def test_search_finds_own_text_and_hides_others(r4_http, enabled):
    """Alice 检得到自己的材料；Bob 用同样的词既看不到正文也看不到命中数；Bob 不能登记 Alice 的文件。"""
    alice, bob = r4_http['alice'], r4_http['bob']
    file = await _upload(alice, '纪要.txt', f'本周例会决定：{SECRET} 下周上线。'.encode())
    registered = await _register(alice, file['id'])
    assert registered.status_code == 201, registered.text
    hits = await _search(alice, SECRET)
    assert hits['total'] == 1 and SECRET in hits['hits'][0]['excerpt']
    assert hits['hits'][0]['revision'] == 1 and hits['hits'][0]['title'] == '纪要.txt'
    assert await _search(bob, SECRET) == {'hits': [], 'total': 0}
    assert (await _register(bob, file['id'])).status_code == 404


async def test_formats_are_checked_by_decoding(r4_http, enabled):
    """非 UTF-8、二进制、坏 JSON、不支持的类型都拒绝；合法 CSV/JSON 可登记。"""
    alice = r4_http['alice']
    bad = [('gbk.txt', '中文'.encode('gbk'), 'text/plain'), ('bin.txt', b'a\x00b', 'text/plain'),
           ('bad.json', b'{not json', 'application/json'), ('doc.pdf', b'%PDF-1.4', 'application/pdf')]
    for name, content, mime in bad:
        file = await _upload(alice, name, content, mime)
        assert (await _register(alice, file['id'])).status_code == 400, name
    for name, content, mime in [('t.csv', '姓名,金额\n张三,100\n'.encode(), 'text/csv'),
                                ('t.json', '{"项目": "预算"}'.encode(), 'application/json')]:
        file = await _upload(alice, name, content, mime)
        assert (await _register(alice, file['id'])).status_code == 201, name


async def test_literal_matching_and_chinese_text(r4_http, enabled):
    """%、_ 按字面匹配，不当通配符；中文连续文本可检；空结果总数为 0。"""
    alice = r4_http['alice']
    file = await _upload(alice, 'rate.txt', '完成率 100% 达标\n编号 A_1 已归档\n'.encode())
    await _register(alice, file['id'])
    assert (await _search(alice, '100%'))['total'] == 1
    assert (await _search(alice, '%'))['total'] == 1
    assert (await _search(alice, 'A_1'))['total'] == 1
    assert (await _search(alice, 'AB1'))['total'] == 0
    assert (await _search(alice, '已归档'))['total'] == 1


async def test_new_revision_is_flagged_and_refresh_is_explicit(r4_http, enabled):
    """原文件出新版本：来源只提示有更新、检索仍是旧版本；明确刷新后换成新版本。"""
    alice = r4_http['alice']
    file = await _upload(alice, 'plan.txt', '旧计划：周三发布'.encode())
    source = (await _register(alice, file['id'])).json()
    changed = await alice.put(f"/v1/workspace/files/{file['id']}/content",
                              params={'request_id': 'v2', 'version': file['version']},
                              content='新计划：周五发布'.encode(), headers={'Content-Type': 'text/plain'})
    assert changed.status_code == 200, changed.text
    listed = (await alice.get('/v1/me/knowledge/sources')).json()['sources']
    assert listed[0]['has_update'] is True and listed[0]['revision'] == 1
    assert (await _search(alice, '周三'))['total'] == 1 and (await _search(alice, '周五'))['total'] == 0
    fresh = await alice.post(f"/v1/me/knowledge/sources/{source['id']}/refresh",
                             json={'expected_version': source['version']})
    assert fresh.status_code == 200 and fresh.json()['revision'] == 2
    assert (await _search(alice, '周三'))['total'] == 0 and (await _search(alice, '周五'))['total'] == 1


async def test_deleted_or_trashed_sources_stop_appearing(r4_http, enabled):
    """删除来源后检不到；原文件移入回收站后来源标 stale、检不到。"""
    alice = r4_http['alice']
    first = await _upload(alice, 'a.txt', '甲方确认'.encode())
    source = (await _register(alice, first['id'])).json()
    deleted = await alice.post(f"/v1/me/knowledge/sources/{source['id']}/delete",
                               json={'expected_version': source['version']})
    assert deleted.status_code == 200 and deleted.json()['status'] == 'deleted'
    assert (await _search(alice, '甲方'))['total'] == 0
    second = await _upload(alice, 'b.txt', '乙方确认'.encode())
    await _register(alice, second['id'])
    trashed = await alice.delete(f"/v1/workspace/items/{second['id']}", params={'version': second['version']})
    assert trashed.status_code in (200, 204), trashed.text
    assert (await _search(alice, '乙方'))['total'] == 0
    states = {s['item_id']: s['status'] for s in (await alice.get('/v1/me/knowledge/sources')).json()['sources']}
    assert states[second['id']] == 'stale'


async def test_switch_off_refuses(r4_http):
    """未开启的企业：登记与检索都 403。"""
    alice = r4_http['alice']
    file = await _upload(alice, 'x.txt', b'hello')
    assert (await _register(alice, file['id'])).status_code == 403
    assert (await alice.post('/v1/me/knowledge/search', json={'query': 'hello'})).status_code == 403


async def _memory(client, path: str, body: dict):
    """调一个工作记忆写接口。"""
    return await client.post('/v1/me/work-memory' + path, json=body)


async def test_work_memory_needs_owner_confirmation(r4_http, enabled):
    """候选须引用本人可读来源、确认才生效；本人陈述标明非已核实；更正生成新修订；删除后不再出现；别人看不到。"""
    alice, bob = r4_http['alice'], r4_http['bob']
    file = await _upload(alice, '规则.txt', '报销单须在 30 天内提交。'.encode())
    source = (await _register(alice, file['id'])).json()
    bad = await _memory(alice, '/proposals', {'text': '报销 30 天', 'citation': {'source_id': source['id'], 'ordinal': 9}})
    assert bad.status_code == 400
    proposed = await _memory(alice, '/proposals', {'text': '报销 30 天内提交',
                                                   'citation': {'source_id': source['id'], 'ordinal': 0}})
    assert proposed.status_code == 201 and proposed.json()['status'] == 'proposed'
    stolen = await _memory(bob, f"/{proposed.json()['id']}/confirm", {'expected_version': 1})
    assert stolen.status_code == 404
    confirmed = await _memory(alice, f"/{proposed.json()['id']}/confirm", {'expected_version': 1})
    assert confirmed.json()['status'] == 'active' and confirmed.json()['verified_by_source'] is True
    mine = await _memory(alice, '/assertions', {'text': '我负责华东区'})
    assert mine.json()['kind'] == 'user_assertion' and mine.json()['verified_by_source'] is False
    corrected = await _memory(alice, f"/{confirmed.json()['id']}/correct",
                              {'expected_version': 2, 'text': '报销 30 个自然日内提交'})
    assert corrected.json()['revision'] == 2 and corrected.json()['lineage'] == confirmed.json()['lineage']
    texts = [m['text'] for m in (await alice.get('/v1/me/work-memory')).json()['memories']]
    assert texts == ['我负责华东区', '报销 30 个自然日内提交']
    await _memory(alice, f"/{mine.json()['id']}/delete", {'expected_version': 1})
    texts = [m['text'] for m in (await alice.get('/v1/me/work-memory')).json()['memories']]
    assert texts == ['报销 30 个自然日内提交']
    assert (await bob.get('/v1/me/work-memory')).json()['memories'] == []


async def test_memory_whose_source_is_deleted_stops_appearing(r4_http, enabled):
    """依据的来源删除后，引用它的记忆不再出现（不靠缓存回填旧内容）。"""
    alice = r4_http['alice']
    file = await _upload(alice, 'f.txt', '会议室 A 周一停用。'.encode())
    source = (await _register(alice, file['id'])).json()
    proposed = (await _memory(alice, '/proposals', {'text': 'A 周一停用',
                                                    'citation': {'source_id': source['id'], 'ordinal': 0}})).json()
    await _memory(alice, f"/{proposed['id']}/confirm", {'expected_version': 1})
    await alice.post(f"/v1/me/knowledge/sources/{source['id']}/delete", json={'expected_version': source['version']})
    assert (await alice.get('/v1/me/work-memory')).json()['memories'] == []

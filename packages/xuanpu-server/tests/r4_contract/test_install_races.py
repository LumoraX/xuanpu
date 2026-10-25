"""同事务写集的并发与异常回滚，不以修改dict模拟PG提交。"""
from __future__ import annotations

import asyncio

import pytest


async def test_two_installations_have_one_cas_winner(r4_http, r4_ids):
    """并发相同预检只能产生一个当前配置修订。"""
    client = r4_http['admin']
    record = (await client.post('/v1/capabilities', json={
        'request_id': 'register', 'delivery': r4_ids['delivery']})).json()
    cap = record['capability_id']
    preview = await client.post(f'/v1/capabilities/{cap}/install-previews',
        json={'request_id': 'preview', 'revision': 1})
    assert preview.status_code == 201, preview.text
    responses = await asyncio.gather(*(client.post(f'/v1/capabilities/{cap}/installations', json={
        'request_id': key, 'preview_id': preview.json()['id'], 'expected_version': 0, 'confirmed': True})
        for key in ('left', 'right')))
    assert sorted(r.status_code for r in responses) == [201, 409]


async def test_capability_history_rolls_back_on_exception(r4_world, r4_ids, r4_http):
    """真实仓储事务异常后，不能留下变更或审计回执。"""
    result = await r4_http['admin'].post('/v1/capabilities', json={
        'request_id': 'register', 'delivery': r4_ids['delivery']})
    assert result.status_code == 201
    store = r4_world.platform.coordination
    async with store.transaction('acme') as tx:
        before = tx.state.capabilities.model_dump_json()
    with pytest.raises(RuntimeError, match='abort'):
        async with store.transaction('acme') as tx:
            tx.state.capabilities.revisions.clear()
            raise RuntimeError('abort')
    async with store.transaction('acme') as tx:
        assert tx.state.capabilities.model_dump_json() == before


async def test_committed_revision_cannot_be_deleted(r4_world, r4_ids, r4_http):
    """绕过HTTP也不能删除不可变记录。"""
    from xuanpu_server.errors import ConflictError
    result = await r4_http['admin'].post('/v1/capabilities', json={
        'request_id': 'register', 'delivery': r4_ids['delivery']})
    assert result.status_code == 201
    with pytest.raises(ConflictError):
        async with r4_world.platform.coordination.transaction('acme') as tx:
            tx.state.capabilities.revisions.clear()

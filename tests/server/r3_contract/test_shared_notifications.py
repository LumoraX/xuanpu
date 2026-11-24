"""来源通知在统一入口呈现，撤权影响内容、总数、重放和已读操作。"""
from __future__ import annotations

import json

from .test_shared_context import invite


async def drain(platform):
    """通过正式恢复消费者补齐已提交事件，不直接伪造通知记录。"""
    assert hasattr(platform, 'shared_notifications'), '共享通知消费者尚未装配'
    for _ in range(20):
        if not await platform.shared_notifications.recover('acme'):
            return
    raise AssertionError('通知未在有界事件数量内完成')


async def test_shared_inbox_current_acl_total_and_replay(shared_http, shared_id, r3_world):
    """不把私有标题存入通知，标读和重放不刷屏，撤权后总数和补读均不可见。"""
    grant = (await invite(shared_http, shared_id, 'notify-reader', 1)).json()
    path = '/v1/shared-matters/' + shared_id
    changed = await shared_http['alice'].patch(path, json={
        'request_id': 'notify-body-change', 'confirmed': True, 'expected_version': 2,
        'title': '通知里不应保存的私有标题', 'content': '通知里不应保存的私有正文'})
    assert changed.status_code == 200, changed.text
    await drain(r3_world.platform)
    first = await shared_http['bob'].get('/v1/notifications', params={'limit': 1})
    assert first.status_code == 200, first.text
    notes = first.json()['shared']
    assert notes['total'] == 2 and notes['unread_total'] == 2 and len(notes['items']) == 1
    assert '私有' not in json.dumps(notes, ensure_ascii=False)
    item = notes['items'][0]
    body = {'request_id': 'read-shared-note', 'confirmed': True, 'expected_version': item['version']}
    url = '/v1/shared-notifications/' + item['id'] + '/read'
    read = await shared_http['bob'].post(url, json=body)
    assert read.status_code == 200, read.text
    assert (await shared_http['bob'].post(url, json=body)).json() == read.json()
    await drain(r3_world.platform)
    notes = (await shared_http['bob'].get('/v1/notifications')).json()['shared']
    assert notes['total'] == 2 and notes['unread_total'] == 1
    assert (await shared_http['tenant_admin'].get('/v1/notifications')).json()['shared']['total'] == 0
    assert (await shared_http['alice'].post(path + '/members/' + grant['id'] + '/revoke', json={
        'request_id': 'notify-reader-revoke', 'confirmed': True, 'expected_version': 3})).status_code == 200
    notes = (await shared_http['bob'].get('/v1/notifications')).json()['shared']
    assert notes['total'] == notes['unread_total'] == 0 and notes['items'] == []
    assert (await shared_http['bob'].post(url, json=body)).status_code == 404


async def test_notification_cursor_tracks_commit_not_source_identifier(shared_http, shared_id, r3_world):
    """后提交来源逐事件确认，新通知按租户锁内分配的顺序继续补读。"""
    await invite(shared_http, shared_id, 'cursor-reader', 1)
    await drain(r3_world.platform)
    previous = (await shared_http['bob'].get('/v1/notifications')).json()['shared']
    first = previous['items'][-1]
    for version in range(2, 6):
        change = await shared_http['alice'].patch('/v1/shared-matters/' + shared_id, json={
            'request_id': 'cursor-change-' + str(version), 'confirmed': True,
            'expected_version': version, 'content': '确认新的资料 ' + str(version)})
        assert change.status_code == 200, change.text
    await drain(r3_world.platform)
    page = (await shared_http['bob'].get('/v1/notifications', params={
        'shared_after_id': first['sequence'], 'limit': 2})).json()['shared']
    assert page['total'] == 5 and len(page['items']) == 2
    assert all(item['sequence'] > first['sequence'] for item in page['items'])
    next_page = (await shared_http['bob'].get('/v1/notifications', params={
        'shared_after_id': page['next_after_id'], 'limit': 2})).json()['shared']
    assert len(next_page['items']) == 2
    assert {item['id'] for item in page['items']}.isdisjoint(item['id'] for item in next_page['items'])


async def test_scope_revocation_before_consumption_never_creates_visible_notification(shared_http, shared_id, r3_world):
    """源授权已撤销但通知尚未消费时，不能先发私有卡片再异步隐藏。"""
    grant = (await invite(shared_http, shared_id, 'offline-reader', 1)).json()
    response = await shared_http['alice'].post('/v1/shared-matters/' + shared_id + '/members/' + grant['id'] + '/revoke', json={
        'request_id': 'offline-revoke', 'confirmed': True, 'expected_version': 2})
    assert response.status_code == 200
    await drain(r3_world.platform)
    notes = (await shared_http['bob'].get('/v1/notifications')).json()['shared']
    assert notes['items'] == [] and notes['total'] == 0

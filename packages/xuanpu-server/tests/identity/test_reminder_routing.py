"""个人提醒是内置能力，语义识别只返回草稿，不提前执行。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


async def test_reminder_route_without_business(identity_world: Any, step: Any) -> None:
    """没有安装任何业务包仍返回待确认提醒，且带服务端时钟。"""
    async with identity_world() as ident:
        user = await ident.user()
        headers = {**ident.cookie(await ident.login(user)), "X-Requested-By": "xuanpu"}
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        ident.world.models.script(step(text=json.dumps({
            "package": "@reminders", "entry": "create", "message": None,
            "reminder": {"title": "准备复盘", "due_at": due.isoformat(), "timezone": "UTC", "repeat": "once"},
        })))
        response = await ident.http.post('/v1/assistant/route', headers=headers,
            json={"text": "一小时后提醒我准备复盘", "timezone": "UTC"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['session'] is None
        assert body['reminder_draft']['title'] == '准备复盘'
        assert body['reminder_draft']['channels'] == ['inbox']
        assert abs(body['reminder_draft']['due_at'] - due.timestamp()) < 1
        assert 'now' in ident.world.models.requests[0].model_dump_json()
        assert not await ident.world.platform.sessions.list_sessions(ident.world.admin)


@pytest.mark.parametrize('bad', ['yesterday', '2020-01-01T08:00:00+08:00', '2030-01-01T08:00:00'])
async def test_reminder_bad_time_cannot_create(identity_world: Any, step: Any, bad: str) -> None:
    """格式错、过去或缺时区均不能返回可创建草稿。"""
    async with identity_world() as ident:
        user = await ident.user()
        headers = {**ident.cookie(await ident.login(user)), "X-Requested-By": "xuanpu"}
        ident.world.models.script(step(text=json.dumps({
            "package": "@reminders", "entry": "create", "message": None,
            "reminder": {"title": "复盘", "due_at": bad, "timezone": "Asia/Shanghai", "repeat": "once"},
        })))
        response = await ident.http.post('/v1/assistant/route', headers=headers, json={"text": "提醒复盘"})
        assert response.json().get('reminder_draft') is None


async def test_reminder_missing_time_clarifies(identity_world: Any, step: Any) -> None:
    """缺时间只追问，不建空任务。"""
    async with identity_world() as ident:
        user = await ident.user()
        headers = {**ident.cookie(await ident.login(user)), "X-Requested-By": "xuanpu"}
        ident.world.models.script(step(text=json.dumps({"package": None, "entry": None, "message": "什么时候提醒你？"})))
        response = await ident.http.post('/v1/assistant/route', headers=headers, json={"text": "提醒我复盘"})
        assert response.status_code == 200
        assert response.json()['message'] == '什么时候提醒你？'
        assert response.json().get('reminder_draft') is None

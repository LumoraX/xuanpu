"""业务分流持久重试、跨实例与保守计量回归。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest
from xuanpu_server.assistant import AssistantRouter, RouteBody
from xuanpu_server.errors import ConflictError
from xuanpu_server.quota import usage_day
from xuanpu_server.tenancy import Principal


@pytest.fixture
async def routed(identity_world):
    """已发布业务的员工及真实模型脚本。"""
    async with identity_world() as ident:
        await ident.world.publish()
        user = await ident.user()
        headers = {**ident.cookie(await ident.login(user)), "X-Requested-By": "xuanpu"}
        who = Principal(role="tenant_member", tenant_id="acme", user_id=user.user_id, key_id=None)
        yield ident, headers, who


@pytest.mark.parametrize("failure", ["append", "outcome"])
async def test_business_route_retry_after_commit_uses_one_session_and_sample(
    routed, step, monkeypatch, failure
):
    """会话提交后响应保存失败，重启路由器仍找回原会话和选择。"""
    ident, headers, _ = routed
    p = ident.world.platform
    target = "append_message" if failure == "append" else "complete_route"
    original = getattr(p.workspace, target)
    failed = False

    async def fail_once(*args, **kwargs):
        """只在创建业务会话后的第一次保存制造故障。"""
        nonlocal failed
        should_fail = failure == "outcome" or args[1] == "assistant"
        if should_fail and not failed:
            failed = True
            raise OSError("simulated_commit_gap")
        return await original(*args, **kwargs)

    monkeypatch.setattr(p.workspace, target, fail_once)
    choice = json.dumps({"package": "support-desk", "entry": "triage", "message": None})
    ident.world.models.script(step(text=choice), step(text=choice))
    body = {"text": "处理工单", "request_id": "durable-business"}
    first = await ident.http.post("/v1/assistant/route", json=body, headers=headers)
    assert first.status_code == 503, first.text
    before = await p.sessions.list_sessions(ident.world.admin)
    assert len(before) == 1
    p.assistant = AssistantRouter(p, ident.world.models)
    second = await ident.http.post("/v1/assistant/route", json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["session"]["session_id"] == before[0].session_id
    assert len(await p.sessions.list_sessions(ident.world.admin)) == 1
    assert len(ident.world.models.requests) == 1


async def test_route_two_instances_do_not_sample_same_request_twice(routed, step):
    """不同路由器进程锁互不共享，持久认领仍只允许一个采样。"""
    ident, _, who = routed
    p = ident.world.platform
    other = AssistantRouter(p, ident.world.models)
    release = asyncio.Event()
    ident.world.models.script(
        step(
            text=json.dumps({"package": "support-desk", "entry": "triage", "message": None}),
            wait=release,
        )
    )
    body = RouteBody(text="处理工单", request_id="two-instances")
    pending = asyncio.create_task(p.assistant.route(who, body))
    while not ident.world.models.requests:
        await asyncio.sleep(0)
    try:
        with pytest.raises(ConflictError):
            await other.route(who, body)
    finally:
        release.set()
    first = await pending
    repeated = await other.route(who, body)
    assert first.session.session_id == repeated.session.session_id
    assert len(ident.world.models.requests) == 1


async def test_cancelled_sample_counts_full_system_and_attachments(routed, step):
    """中断文本和多模态附件不能绕过用量估算，供应商数字仍优先。"""
    ident, _, who = routed
    p = ident.world.platform
    day = usage_day(p._now())
    charges = []
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 99992
    image = {
        "type": "image",
        "media_type": "image/png",
        "size": len(data),
        "base64_data": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    for parts in (None, [{"type": "text", "text": "中" * 16000}], [image]):
        old = await p.store.get_usage("acme", day)
        ident.world.models.script(step(wait=asyncio.Event()))
        count = len(ident.world.models.requests)
        task = asyncio.create_task(p.assistant._sample(who, "same-small-payload", parts))
        while len(ident.world.models.requests) == count:
            if task.done():
                await task
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        new = await p.store.get_usage("acme", day)
        charges.append(new.input_tokens - old.input_tokens)
    assert charges[1] >= charges[0] + 16000
    assert charges[2] > charges[0]


async def test_invalid_choice_retry_same_id_can_resample(routed, step):
    """没有产生会话的无效模型选择不能永久冻结同一请求。"""
    ident, headers, _ = routed
    ident.world.models.script(
        step(text=json.dumps({"package": "unknown", "entry": "bad", "message": None})),
        step(text=json.dumps({"package": "support-desk", "entry": "triage", "message": None})),
    )
    body = {"text": "处理工单", "request_id": "invalid-choice-retry"}
    assert (
        await ident.http.post("/v1/assistant/route", json=body, headers=headers)
    ).status_code == 503
    retried = await ident.http.post("/v1/assistant/route", json=body, headers=headers)
    assert retried.status_code == 200, retried.text
    assert len(ident.world.models.requests) == 2


async def test_created_session_recovery_survives_active_version_change(routed, step, monkeypatch):
    """会话已创建后切版，重试仍恢复原来钉住版本的会话。"""
    ident, headers, _ = routed
    p = ident.world.platform
    original = p.workspace.complete_route
    failed = False

    async def fail_once(*args, **kwargs):
        """在会话已提交后中断结果回执。"""
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("interrupted")
        return await original(*args, **kwargs)

    monkeypatch.setattr(p.workspace, "complete_route", fail_once)
    ident.world.models.script(
        step(text=json.dumps({"package": "support-desk", "entry": "triage", "message": None}))
    )
    body = {"text": "处理工单", "request_id": "before-deploy"}
    assert (
        await ident.http.post("/v1/assistant/route", json=body, headers=headers)
    ).status_code == 503
    first = (await p.sessions.list_sessions(ident.world.admin))[0]
    await ident.world.publish(version="2.0.0")
    retry = await ident.http.post("/v1/assistant/route", json=body, headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["session"]["session_id"] == first.session_id
    assert retry.json()["session"]["version"] == first.version
    assert len(ident.world.models.requests) == 1


async def test_precreation_version_change_retries_latest_catalog(routed, step, monkeypatch):
    """采样后但创建前切版，旧选择可丢弃并在同请求下重采样最新目录。"""
    ident, headers, _ = routed
    p = ident.world.platform
    original = p.assistant._sample
    changed = False

    async def change_after_sample(*args, **kwargs):
        """第一次采样完成后切换业务版本。"""
        nonlocal changed
        text = await original(*args, **kwargs)
        if not changed:
            changed = True
            await ident.world.publish(version="2.0.0")
        return text

    monkeypatch.setattr(p.assistant, "_sample", change_after_sample)
    choice = json.dumps({"package": "support-desk", "entry": "triage", "message": None})
    ident.world.models.script(step(text=choice), step(text=choice))
    body = {"text": "处理工单", "request_id": "switch-before-create"}
    assert (
        await ident.http.post("/v1/assistant/route", json=body, headers=headers)
    ).status_code == 503
    assert await p.sessions.list_sessions(ident.world.admin) == []
    retry = await ident.http.post("/v1/assistant/route", json=body, headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["session"]["version"] == "2.0.0"
    assert len(ident.world.models.requests) == 2

"""已发布子Skill真实挂起经公开cookie HTTP恢复，禁止根线程误核销叶待办。"""
from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from xuanpu_server.api import create_app
from xuanpu_spec import pack_package


@asynccontextmanager
async def records_server():
    """启动真实loopback写接口，记录实际副作用次数。"""
    app, writes = FastAPI(), []

    @app.post('/records')
    async def append() -> dict[str, bool]:
        """真实请求抵达后才产生本测试的单一副作用。"""
        writes.append('append')
        return {'saved': True}

    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                          log_level='critical', access_log=False))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.01)
        yield f'http://127.0.0.1:{port}', writes
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def publish_nested(ident: Any, directory: Path, url: str, *, depth: int = 2) -> None:
    """发布真实父子Skill和进程外HTTP工具，不手工伪造挂起审计。"""
    for name in ['main', 'child']:
        (directory / 'skills' / name).mkdir(parents=True)
    (directory / 'xuanpu.yaml').write_text('''apiVersion: xuanpu/v1
metadata: {name: nested-approval, version: 1.0.0}
spec:
  skills: {entries: [main]}
  policy: {approval: standard}
  connectors:
  - {name: records, transport: http, openapi: api.json, baseUrl: "''' + url + '"}\n')
    (directory / 'api.json').write_text(json.dumps({'openapi': '3.1.0',
        'info': {'title': '合成记录'}, 'paths': {'/records': {'post': {'operationId': 'append'}}}}))
    (directory / 'skills/main/SKILL.md').write_text('''---
name: main
description: 合成父入口
version: 1.0.0
entry: true
type: composite
child_skills: [child]
---
交子Skill保存。
''')
    (directory / 'skills/child/SKILL.md').write_text('''---
name: child
description: 合成写入子任务
version: 1.0.0
type: composite
tool_names: [api__records__append]
---
保存合成记录。
''')
    if depth == 3:
        child = directory / 'skills/child/SKILL.md'
        child.write_text(child.read_text().replace('tool_names:', 'child_skills: [leaf]\ntool_names:'))
        (directory / 'skills/leaf').mkdir()
        (directory / 'skills/leaf/SKILL.md').write_text(
            child.read_text().replace('name: child', 'name: leaf').replace('child_skills: [leaf]\n', ''))
    await ident.world.platform.packages.publish(ident.world.admin, pack_package(directory), activate=True)


def events(response: httpx.Response) -> list[dict[str, Any]]:
    """读取实际公开HTTP的SSE数据帧。"""
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


async def suspended(ident: Any, step: Any) -> tuple[str, dict[str, str], dict[str, Any], list[Any]]:
    """真实模型协议夹具派发子Skill并触发写工具挂起。"""
    person = await ident.user(name='子任务申请人')
    headers = {**ident.cookie(await ident.login(person)), 'X-Requested-By': 'xuanpu'}
    response = await ident.http.post('/v1/sessions', headers=headers,
                                     json={'package': 'nested-approval'})
    assert response.status_code == 201, response.text
    sid = response.json()['session_id']
    ident.world.models.script(
        step(calls=[('delegate', 'call_skill', {'skill_id': 'child', 'reason': '保存真实记录'})]),
        step(calls=[('write', 'api__records__append', {})]),
        step(text='子任务已结束'), step(text='根任务已结束'))
    data = events(await ident.http.post(f'/v1/sessions/{sid}/messages', headers=headers,
                                       json={'text': '保存一条合成记录'}))
    pending = (await ident.http.get(f'/v1/sessions/{sid}/pending', headers=headers)).json()['pending']
    assert len(pending) == 1
    assert pending[0]['detail']['target'] == 'api__records__append'
    return sid, headers, pending[0], data


@pytest.mark.parametrize('cold', [False, True])
@pytest.mark.parametrize('granted', [False, True])
async def test_nested_cookie_resume_normal_cold_deny_and_replay(
    identity_world: Any, step: Any, tmp_path: Path, cold: bool, granted: bool,
) -> None:
    """真实父子挂起在正常或冷恢复后批准/拒绝，重复恢复不重复执行。"""
    async with identity_world() as ident, records_server() as (url, writes):
        await publish_nested(ident, tmp_path / 'nested', url)
        sid, headers, pending, emitted = await suspended(ident, step)
        leaf = next(item['data'] for item in emitted if item.get('kind') == 'turn_suspended')
        assert leaf['thread_id'] == pending['detail']['thread_id']
        assert writes == []
        platform, http = ident.world.platform, ident.http
        sibling = None
        if cold:
            await platform.close()
            sibling = ident.world.sibling('nested-cold', data_dir=ident.world.data_dir)
            await sibling.start()
            platform = sibling
            http = httpx.AsyncClient(transport=httpx.ASGITransport(create_app(sibling)),
                                     base_url=str(ident.http.base_url))
        audit = await ident.audit('turn_suspended')
        assert [item.data['pending'][0]['reason'] for item in audit] == ['permission', 'child_skill']
        payload = {'resolutions': {pending['request_id']: {'granted': granted}}}
        try:
            resumed = events(await http.post(f'/v1/sessions/{sid}/resume', headers=headers, json=payload))
            rejected = [item for item in resumed if item.get('kind') == 'suspension_resolve_rejected']
            assert rejected == [], rejected
            assert any(item.get('kind') == 'turn_completed' and item['data'].get('is_root')
                       and item['data'].get('success') for item in resumed), resumed
            assert writes == (['append'] if granted else [])
            sampled = len(ident.world.models.requests)
            stamp = next(item['at'] for item in reversed(resumed) if item.get('kind') == 'turn_completed')
            usage = await platform.store.get_usage('acme', datetime.fromtimestamp(stamp, UTC).date().isoformat())
            assert usage.turns == 2
            assert usage.total_tokens == sampled * 120
            duplicate = await http.post(f'/v1/sessions/{sid}/resume', headers=headers, json=payload)
            assert duplicate.headers.get('X-Xuanpu-Replayed') == 'true'
            assert len(ident.world.models.requests) == sampled
            assert writes == (['append'] if granted else [])
        finally:
            if sibling:
                await http.aclose()
                await sibling.close()


class ParentAppendGate:
    """只包装公开MessageStore，确定性拦住父挂起记录落盘，不修改内核。"""

    def __init__(self, directory: Path) -> None:
        """用实际JSONL保存，门闩只控制纯child_skill父记录的写入时点。"""
        from taifeng import JsonlMessageStore

        self.inner = JsonlMessageStore(directory)
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def create_thread(self, **kwargs: Any) -> str:
        """逐字委托真实线程创建。"""
        return await self.inner.create_thread(**kwargs)

    async def load_thread(self, thread_id: str) -> Any:
        """逐字委托真实持久历史读取。"""
        return await self.inner.load_thread(thread_id)

    async def append_batch(self, items: list[Any]) -> None:
        """批次写仍经过同一受控单条门闩。"""
        for item in items:
            await self.append(item)

    async def list_threads(self, **kwargs: Any) -> Any:
        """逐字委托真实线程目录查询。"""
        return await self.inner.list_threads(**kwargs)

    async def select_resume_path(self, cwd: str) -> str | None:
        """逐字委托真实恢复位置选择。"""
        return await self.inner.select_resume_path(cwd)

    async def close(self) -> None:
        """关闭真实存储，不虚构内核终态。"""
        await self.inner.close()

    async def append(self, item: Any) -> None:
        """父记录写入前停住，子记录和其真实事件已经完成。"""
        pending = item.payload.get('pending', []) if item.kind == 'suspension' else []
        if pending and all(request['reason'] == 'child_skill' for request in pending):
            self.entered.set()
            await self.release.wait()
        await self.inner.append(item)


async def test_leaf_sse_waits_for_parent_suspension_persistence(
    world_opener: Any, local_sandboxes: Any, step: Any, tmp_path: Path,
) -> None:
    """父挂起未落盘时初始SSE不能先关闭，避免立即冷关闭丢失可恢复链。"""
    from types import SimpleNamespace

    from xuanpu_server.controlplane import InMemoryControlPlane
    from xuanpu_server.runtime import StateBackends

    gate = ParentAppendGate(tmp_path / 'gated-threads')

    class States:
        """首次用门闩存储，冷恢复创建全新公开存储实例。"""

        def __init__(self) -> None:
            """只让首次运行时使用受控门闩。"""
            self.opened = False

        def open_store(self) -> Any:
            """第二次必须从JSONL重建，不能沿用已关闭实例的内存状态。"""
            from taifeng import JsonlMessageStore

            if not self.opened:
                self.opened = True
                return gate
            return JsonlMessageStore(tmp_path / 'gated-threads')

        async def for_tenant(self, tenant_id: str) -> StateBackends:
            """本用例仅acme一个租户，完全隔离于其他状态面。"""
            assert tenant_id == 'acme'
            return StateBackends(message_store=self.open_store)

    world = await world_opener(store=InMemoryControlPlane(), sandboxes=local_sandboxes(),
                               states=States())
    try:
        async with records_server() as (url, writes), httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(world.platform)), base_url='http://xuanpu',
        ) as http:
            await publish_nested(SimpleNamespace(world=world), tmp_path / 'gated-package', url)
            headers = {'Authorization': 'Bearer ' + world.member_key}
            response = await http.post('/v1/sessions', headers=headers,
                                       json={'package': 'nested-approval'})
            sid = response.json()['session_id']
            world.models.script(step(calls=[('delegate', 'call_skill',
                {'skill_id': 'child', 'reason': '真实写入'})]),
                step(calls=[('write', 'api__records__append', {})]))
            request = asyncio.create_task(http.post(f'/v1/sessions/{sid}/messages',
                                                   headers=headers, json={'text': '保存'}))
            await asyncio.wait_for(gate.entered.wait(), 5)
            try:
                # 这是受控门闩下的完成性断言，不靠睡眠等后台“碰巧”追上。
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(request), .1)
            finally:
                gate.release.set()
                await asyncio.wait_for(request, 5)
            await cold_resume_after_gate(world, sid, headers, step, writes)
    finally:
        gate.release.set()
        await world.platform.close()


async def test_terminal_waits_for_pending_audit_flush(
    world_opener: Any, local_sandboxes: Any, step: Any, tmp_path: Path,
) -> None:
    """终态可见前同会话leaf待办必须已落审计，立刻GET不读陈旧空结果。"""
    from types import SimpleNamespace

    from xuanpu_server.controlplane import InMemoryControlPlane

    class AuditGate(InMemoryControlPlane):
        """明确暂停真实审计写入，其他控制面行为保持实际实现。"""

        def __init__(self) -> None:
            """建立门闩，尚未发生挂起时不阻塞初始化。"""
            super().__init__()
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def append_audit(self, records: Any) -> int:
            """带真实挂起的批次必须等待门闩后才落实际审计。"""
            if any(item.kind == 'turn_suspended' for item in records):
                self.entered.set()
                await self.release.wait()
            return await super().append_audit(records)

    store = AuditGate()
    world = await world_opener(store=store, sandboxes=local_sandboxes())
    try:
        async with records_server() as (url, _), httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(world.platform)), base_url='http://xuanpu',
        ) as http:
            await publish_nested(SimpleNamespace(world=world), tmp_path / 'audit-package', url)
            headers = {'Authorization': 'Bearer ' + world.member_key}
            response = await http.post('/v1/sessions', headers=headers,
                                       json={'package': 'nested-approval'})
            sid = response.json()['session_id']
            world.models.script(step(calls=[('delegate', 'call_skill',
                {'skill_id': 'child', 'reason': '真实写入'})]),
                step(calls=[('write', 'api__records__append', {})]))
            request = asyncio.create_task(http.post(f'/v1/sessions/{sid}/messages',
                                                   headers=headers, json={'text': '保存'}))
            await asyncio.wait_for(store.entered.wait(), 5)
            try:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(request), .1)
            finally:
                store.release.set()
                result = await asyncio.wait_for(request, 5)
            assert events(result)[-2]['kind'] == 'turn_suspended'
            pending = (await http.get(f'/v1/sessions/{sid}/pending', headers=headers)).json()['pending']
            assert len(pending) == 1 and pending[0]['reason'] == 'permission'
    finally:
        store.release.set()
        await world.platform.close()


async def test_three_level_resume_parent_dispatches_new_child_and_resuspends(
    identity_world: Any, step: Any, tmp_path: Path,
) -> None:
    """叶A恢复后父B派生新叶C，必须等B新wrapper落盘再终结且可继续恢复。"""
    async with identity_world() as ident, records_server() as (url, writes):
        await publish_nested(ident, tmp_path / 'deep', url, depth=3)
        user = await ident.user(name='三层申请人')
        headers = {**ident.cookie(await ident.login(user)), 'X-Requested-By': 'xuanpu'}
        response = await ident.http.post('/v1/sessions', headers=headers,
                                         json={'package': 'nested-approval'})
        sid = response.json()['session_id']
        ident.world.models.script(
            step(calls=[('root-child', 'call_skill', {'skill_id': 'child', 'reason': '分派'})]),
            step(calls=[('first-leaf', 'call_skill', {'skill_id': 'leaf', 'reason': '首次保存'})]),
            step(calls=[('first-write', 'api__records__append', {})]))
        initial = events(await ident.http.post(f'/v1/sessions/{sid}/messages', headers=headers,
                                               json={'text': '保存两次'}))
        suspensions = [item['data'] for item in initial if item.get('kind') == 'turn_suspended']
        assert len(suspensions) == 3
        parent_thread = suspensions[1]['thread_id']
        pending = (await ident.http.get(f'/v1/sessions/{sid}/pending', headers=headers)).json()['pending']
        first_leaf = pending[0]['detail']['thread_id']
        ident.world.models.script(step(text='首次保存已完成'),
            step(calls=[('next-leaf', 'call_skill', {'skill_id': 'leaf', 'reason': '再次保存'})]),
            step(calls=[('next-write', 'api__records__append', {})]))
        async with asyncio.timeout(5):
            resumed = events(await ident.http.post(f'/v1/sessions/{sid}/resume', headers=headers,
                json={'resolutions': {pending[0]['request_id']: {'granted': True}}}))
        final_suspend = [item['data'] for item in resumed if item.get('kind') == 'turn_suspended'][-1]
        assert final_suspend['thread_id'] == parent_thread
        pending = (await ident.http.get(f'/v1/sessions/{sid}/pending', headers=headers)).json()['pending']
        assert len(pending) == 1 and pending[0]['detail']['thread_id'] != first_leaf
        ident.world.models.script(step(text='再次保存已完成'), step(text='父任务已完成'), step(text='根任务已完成'))
        done = events(await ident.http.post(f'/v1/sessions/{sid}/resume', headers=headers,
            json={'resolutions': {pending[0]['request_id']: {'granted': True}}}))
        assert any(item.get('kind') == 'turn_completed' and item['data'].get('is_root') for item in done)
        assert writes == ['append', 'append']
        await assert_nested_usage(ident, done, 3)


async def test_nested_resume_ownership_unknown_batch_and_thread_injection(
    identity_world: Any, step: Any, tmp_path: Path,
) -> None:
    """其他员工、未知混批及客户端线程字段均不能执行当前叶写操作。"""
    async with identity_world() as ident, records_server() as (url, writes):
        await publish_nested(ident, tmp_path / 'ownership', url)
        sid, owner, pending, _ = await suspended(ident, step)
        stranger = await ident.user(name='其他员工')
        foreign = {**ident.cookie(await ident.login(stranger)), 'X-Requested-By': 'xuanpu'}
        payload = {'resolutions': {pending['request_id']: {'granted': True}}}
        response = await ident.http.post(f'/v1/sessions/{sid}/resume', headers=foreign, json=payload)
        assert response.status_code == 404 and writes == []
        response = await ident.http.post(f'/v1/sessions/{sid}/resume', headers=owner,
                                         json={**payload, 'thread_id': 'foreign-thread'})
        assert response.status_code == 400 and writes == []
        malformed = {'resolutions': {**payload['resolutions'], 'foreign-request': {'granted': True}}}
        result = events(await ident.http.post(f'/v1/sessions/{sid}/resume', headers=owner, json=malformed))
        assert any(item.get('kind') == 'suspension_resolve_rejected' for item in result)
        assert writes == []
        still = (await ident.http.get(f'/v1/sessions/{sid}/pending', headers=owner)).json()['pending']
        assert still[0]['request_id'] == pending['request_id']
        result = events(await ident.http.post(f'/v1/sessions/{sid}/resume', headers=owner, json=payload))
        assert any(item.get('kind') == 'turn_completed' and item['data'].get('is_root') for item in result)
        assert writes == ['append']


def test_internal_wrapper_filter_preserves_mixed_human_pending_and_bad_thread() -> None:
    """只过滤纯内部父挂起，混合人工记录和已知坏线程不能静默吞掉。"""
    from xuanpu_server.errors import InvalidRequestError
    from xuanpu_server.runtime.session_history import internal_wrapper, suspension_thread

    assert internal_wrapper({'pending': [{'reason': 'child_skill'}]})
    assert not internal_wrapper({'pending': [{'reason': 'child_skill'}, {'reason': 'permission'}]})
    assert suspension_thread({}) is None
    with pytest.raises(InvalidRequestError, match='挂起线程标识无效'):
        suspension_thread({'thread_id': ''})


async def test_nested_sdk_terminal_boundary_live_and_audit_replay(
    identity_world: Any, step: Any, tmp_path: Path,
) -> None:
    """SDK真实消费初始leaf到根及恢复完成，live与审计投影一致且可提前停在操作终态。"""
    from xuanpu_sdk import Event, XuanpuClient

    async with identity_world() as ident, records_server() as (url, writes):
        await publish_nested(ident, tmp_path / 'sdk-nested', url)
        transport = httpx.ASGITransport(create_app(ident.world.platform))
        async with XuanpuClient('http://xuanpu', ident.world.member_key,
                                transport=transport) as client:
            session = await client.sessions.create('nested-approval')
            sid = session.session_id
            ident.world.models.script(
                step(calls=[('delegate', 'call_skill', {'skill_id': 'child', 'reason': '保存'})]),
                step(calls=[('write', 'api__records__append', {})]),
                step(text='子任务完成'), step(text='根任务完成'))
            emitted = []
            async for item in client.sessions.send(sid, '保存一条合成记录'):
                emitted.append(item)
                if item.terminal:
                    break
            hung = [item for item in emitted if item.kind == 'turn_suspended']
            assert [item.data['operation_terminal'] for item in hung] == [False, True]
            assert [item.terminal for item in hung] == [False, True]
            pending = await client.sessions.pending(sid)
            assert len(pending) == 1 and pending[0]['reason'] == 'permission'
            replay = await client.sessions.replay(sid, hung[-1].submission_id)
            saved = [item for item in replay if item['kind'] == 'turn_suspended']
            assert [item['data']['operation_terminal'] for item in saved] == [False, True]
            assert all(item.data == row['data'] for item, row in zip(hung, saved, strict=True))
            resumed = [item async for item in client.sessions.resume(
                sid, {pending[0]['request_id']: {'granted': True}})]
            assert resumed[-1].terminal and resumed[-1].kind == 'turn_completed'
            assert writes == ['append']
            duplicate = [item async for item in client.sessions.resume(
                sid, {pending[0]['request_id']: {'granted': True}})]
            assert any(item.terminal and item.kind == 'turn_completed' for item in duplicate)
            assert duplicate[-1].kind == 'turn_text' and not duplicate[-1].terminal
            assert writes == ['append']
            assert all(Event(kind=row['kind'], submission_id=hung[-1].submission_id,
                       seq=row['seq'], at=row['at'], data=row['data']).terminal == item.terminal
                       for row, item in zip([row for row in saved], hung, strict=True))


def test_operation_terminal_projection_overrides_same_named_kernel_data() -> None:
    """平台可信终态最后覆盖同名字段，显式False保留且不修改内核对象。"""
    from types import SimpleNamespace

    from xuanpu_server.api.sse import event_payload

    original = {'operation_terminal': True, 'thread_id': 'leaf'}
    event = SimpleNamespace(submission_id='sub', seq=2, timestamp=datetime.now(UTC),
                            msg=SimpleNamespace(kind='turn_suspended', data=original))
    projected = event_payload(event, operation_terminal=False)
    assert projected['data']['operation_terminal'] is False
    assert original['operation_terminal'] is True


async def cold_resume_after_gate(world: Any, sid: str, headers: dict[str, str],
                                  step: Any, writes: list[str]) -> None:
    """门闩释放并收到SSE后立刻关闭进程状态，公开冷恢复真实叶待办。"""
    await world.platform.close()
    sibling = world.sibling('gated-cold', data_dir=world.data_dir)
    await sibling.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(sibling)),
                                    base_url='http://xuanpu') as http:
            pending = (await http.get(f'/v1/sessions/{sid}/pending', headers=headers)).json()['pending']
            assert len(pending) == 1 and pending[0]['reason'] == 'permission'
            world.models.script(step(text='冷恢复子任务完成'), step(text='冷恢复根任务完成'))
            result = events(await http.post(f'/v1/sessions/{sid}/resume', headers=headers,
                json={'resolutions': {pending[0]['request_id']: {'granted': True}}}))
            assert result[-2]['kind'] == 'turn_completed' and result[-2]['data']['is_root']
            assert result[-2]['data']['operation_terminal'] is True
            assert writes == ['append']
            assert (await http.get(f'/v1/sessions/{sid}/pending', headers=headers)).json()['pending'] == []
    finally:
        await sibling.close()


async def assert_nested_usage(ident: Any, result: list[dict[str, Any]], turns: int) -> None:
    """按真实采样数与操作数核计量，叶与父终态不能重复计费或丢用量。"""
    stamp = next(item['at'] for item in reversed(result) if 'at' in item)
    usage = await ident.world.platform.store.get_usage(
        'acme', datetime.fromtimestamp(stamp, UTC).date().isoformat())
    assert usage.turns == turns
    assert usage.total_tokens == len(ident.world.models.requests) * 120


async def test_resumed_leaf_itself_suspends_again_then_completes(
    identity_world: Any, step: Any, tmp_path: Path,
) -> None:
    """同一叶恢复后再次写而挂起，操作在该叶停止且后续仍可恢复到根完成。"""
    async with identity_world() as ident, records_server() as (url, writes):
        await publish_nested(ident, tmp_path / 'same-leaf', url)
        headers = {'Authorization': 'Bearer ' + ident.world.member_key}
        response = await ident.http.post('/v1/sessions', headers=headers,
                                         json={'package': 'nested-approval'})
        sid = response.json()['session_id']
        ident.world.models.script(
            step(calls=[('delegate', 'call_skill', {'skill_id': 'child', 'reason': '分次写入'})]),
            step(calls=[('first-write', 'api__records__append', {})]),
            step(calls=[('second-write', 'api__records__append', {})]),
            step(text='叶任务完成'), step(text='根任务完成'))
        events(await ident.http.post(f'/v1/sessions/{sid}/messages', headers=headers, json={'text': '保存两次'}))
        first = (await ident.http.get(f'/v1/sessions/{sid}/pending', headers=headers)).json()['pending'][0]
        async with asyncio.timeout(5):
            resumed = events(await ident.http.post(f'/v1/sessions/{sid}/resume', headers=headers,
                json={'resolutions': {first['request_id']: {'granted': True}}}))
        last = [item for item in resumed if item.get('kind') == 'turn_suspended'][-1]
        assert last['data']['thread_id'] == first['detail']['thread_id']
        assert last['data']['operation_terminal'] is True and writes == ['append']
        await assert_public_terminal_replay(ident.http, sid, headers, resumed)
        assert ident.world.platform.sessions.in_flight_turns('acme') == 0
        second = (await ident.http.get(f'/v1/sessions/{sid}/pending', headers=headers)).json()['pending'][0]
        assert second['request_id'] != first['request_id']
        assert second['detail']['thread_id'] == first['detail']['thread_id']
        result = events(await ident.http.post(f'/v1/sessions/{sid}/resume', headers=headers,
            json={'resolutions': {second['request_id']: {'granted': True}}}))
        assert result[-2]['kind'] == 'turn_completed' and result[-2]['data']['is_root']
        assert writes == ['append', 'append']
        await assert_nested_usage(ident, result, 3)


async def test_nested_leaf_partial_resolution_then_remaining_batch(
    identity_world: Any, step: Any, tmp_path: Path,
) -> None:
    """叶批次只核销一项即结束本操作，余项仍待确认且重复回放不重复写。"""
    async with identity_world() as ident, records_server() as (url, writes):
        await publish_nested(ident, tmp_path / 'leaf-partial', url)
        headers = {'Authorization': 'Bearer ' + ident.world.member_key}
        response = await ident.http.post('/v1/sessions', headers=headers,
                                         json={'package': 'nested-approval'})
        sid = response.json()['session_id']
        ident.world.models.script(
            step(calls=[('delegate', 'call_skill', {'skill_id': 'child', 'reason': '批次保存'})]),
            step(calls=[('write-a', 'api__records__append', {}), ('write-b', 'api__records__append', {})]),
            step(text='叶批次完成'), step(text='根任务完成'))
        events(await ident.http.post(f'/v1/sessions/{sid}/messages', headers=headers, json={'text': '保存两条'}))
        first, second = (await ident.http.get(f'/v1/sessions/{sid}/pending', headers=headers)).json()['pending']
        assert first['detail']['thread_id'] == second['detail']['thread_id']
        payload = {'resolutions': {first['request_id']: {'granted': True}}}
        async with asyncio.timeout(5):
            partial = events(await ident.http.post(f'/v1/sessions/{sid}/resume', headers=headers, json=payload))
        assert partial[-2]['kind'] == 'suspension_partially_resolved'
        assert partial[-2]['data']['operation_terminal'] is True
        await assert_public_terminal_replay(ident.http, sid, headers, partial)
        assert writes == ['append'] and len(ident.world.models.requests) == 2
        assert ident.world.platform.sessions.in_flight_turns('acme') == 0
        remaining = (await ident.http.get(f'/v1/sessions/{sid}/pending', headers=headers)).json()['pending']
        assert [item['request_id'] for item in remaining] == [second['request_id']]
        replay = await ident.http.post(f'/v1/sessions/{sid}/resume', headers=headers, json=payload)
        assert replay.headers.get('X-Xuanpu-Replayed') == 'true' and writes == ['append']
        assert events(replay)[-2]['data']['operation_terminal'] is True
        done = events(await ident.http.post(f'/v1/sessions/{sid}/resume', headers=headers,
            json={'resolutions': {second['request_id']: {'granted': True}}}))
        assert done[-2]['kind'] == 'turn_completed' and done[-2]['data']['is_root']
        assert writes == ['append', 'append']
        await assert_nested_usage(ident, done, 2)


async def assert_public_terminal_replay(http: Any, sid: str, headers: dict[str, str],
                                        emitted: list[dict[str, Any]]) -> None:
    """当前操作的真实SSE与公开审计回放逐项核对终态标记，不只看最终状态。"""
    kinds = {'turn_suspended', 'turn_completed', 'turn_failed', 'suspension_partially_resolved'}
    live = [item for item in emitted if item.get('kind') in kinds]
    response = await http.get(f'/v1/sessions/{sid}/events', headers=headers,
                              params={'submission_id': live[-1]['submission_id']})
    assert response.status_code == 200
    saved = [item for item in response.json()['events'] if item['kind'] in kinds]
    assert [(item['kind'], item['seq'], item['data']['operation_terminal']) for item in live] == [
        (item['kind'], item['seq'], item['data']['operation_terminal']) for item in saved]

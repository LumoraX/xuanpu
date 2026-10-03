"""对账按通讯录停用并保留查不到的员工。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from xuanpu_server.identity import ExternalIdentity
from xuanpu_server.identity.feishu import FeishuClient


@pytest.mark.parametrize(
    "flag,reason",
    [
        ("is_resigned", "feishu_resigned"),
        ("is_frozen", "feishu_frozen"),
        ("is_exited", "feishu_exited"),
    ],
)
async def test_reconcile_status(identity_world: Any, flag: str, reason: str) -> None:
    """三种明确状态走停用流水线。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        await ident.store.bind_tenant_key("acme", "feishu", "tk_acme")
        user = await ident.user(role="tenant_admin")
        await ident.store.create_external_identity(
            ExternalIdentity(
                tenant_id="acme", provider="feishu", subject="union", user_id=user.user_id
            )
        )

        def handle(request: httpx.Request) -> httpx.Response:
            """模拟通讯录。"""
            if request.method == "POST":
                return httpx.Response(
                    200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [{"union_id": "union", "status": {flag: True}}]},
                },
            )

        service = ident.world.platform.identity
        assert hasattr(service, "reconcile")
        service.reconcile.feishu = FeishuClient(
            "https://open.feishu.cn",
            "https://accounts.feishu.cn",
            timeout_seconds=10,
            transport=httpx.MockTransport(handle),
        )
        try:
            counts = await service.reconcile.run("acme", manual=True)
            assert counts == {"checked": 1, "disabled": 1, "unverified": 0}
            current = await ident.store.get_user("acme", user.user_id)
            assert current.disabled_reason == reason
            state = await ident.store.get_reconcile_state("acme")
            assert state.last_run_at is not None and state.lease_owner is None
        finally:
            await service.reconcile.feishu.aclose()


@pytest.mark.parametrize(
    "status", [{}, {"is_frozen": False, "is_resigned": False, "is_exited": False}]
)
async def test_normal_and_missing_status(identity_world: Any, status: dict[str, bool]) -> None:
    """正常员工及缺失状态都不推断离职。"""
    async with identity_world() as ident:
        await ident.configure_feishu()
        await ident.store.bind_tenant_key("acme", "feishu", "tk_acme")
        user = await ident.user()
        await ident.store.create_external_identity(
            ExternalIdentity(tenant_id="acme", provider="feishu", subject="u", user_id=user.user_id)
        )

        def handle(request: httpx.Request) -> httpx.Response:
            """返回明确正常或没有状态的员工。"""
            if request.method == "POST":
                return httpx.Response(
                    200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
                )
            return httpx.Response(
                200, json={"code": 0, "data": {"items": [{"union_id": "u", "status": status}]}}
            )

        async with directory(ident, handle) as runner:
            assert await runner.run("acme") == {"checked": 1, "disabled": 0, "unverified": 0}
        assert (await ident.store.get_user("acme", user.user_id)).status == "active"


import asyncio
from contextlib import asynccontextmanager

from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.errors import ConflictError, RuntimeUnavailableError
from xuanpu_server.identity.reconcile import IdentityReconcile

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


@asynccontextmanager
async def directory(ident: Any, handle: Callable[..., Any]) -> AsyncIterator[Any]:
    """仅替换通讯录传输，保留真实服务、存储、限流和停用链。"""
    runner = ident.world.platform.identity.reconcile
    original = runner.feishu
    runner.feishu = FeishuClient(
        "https://open.feishu.cn",
        "https://accounts.feishu.cn",
        timeout_seconds=10,
        transport=httpx.MockTransport(handle),
    )
    try:
        yield runner
    finally:
        await runner.feishu.aclose()
        runner.feishu = original


async def prepared(ident: Any, n: int = 1) -> list[Any]:
    """配置并创建连续 subject，方便检查翻页不漏人。"""
    await ident.configure_feishu()
    await ident.store.bind_tenant_key("acme", "feishu", "tk_acme")
    users = []
    for i in range(n):
        user = await ident.user()
        await ident.store.create_external_identity(
            ExternalIdentity(
                tenant_id="acme", provider="feishu", subject=f"u{i:03}", user_id=user.user_id
            )
        )
        users.append(user)
    return users


def token_response() -> httpx.Response:
    """模拟应用令牌。"""
    return httpx.Response(200, json={"code": 0, "tenant_access_token": "token", "expire": 7200})


async def test_unverified_clear_and_token_cache(identity_world: Any) -> None:
    """查不到只标记一次；再查到清标记，提前五分钟刷新令牌，改密钥也刷新。"""
    async with identity_world() as ident:
        users = await prepared(ident)
        visible = False
        tokens = 0

        def handle(request: httpx.Request) -> httpx.Response:
            """模拟可见范围和令牌申请次数。"""
            nonlocal tokens
            if request.method == "POST":
                tokens += 1
                return token_response()
            items = [{"union_id": "u000", "status": {}}] if visible else []
            return httpx.Response(200, json={"code": 0, "data": {"items": items}})

        async with directory(ident, handle) as runner:
            clock = [1000.0]
            runner._now = lambda: clock[0]
            await runner.run("acme", manual=True)
            first = await ident.store.get_user("acme", users[0].user_id)
            clock[0] += 1
            await runner.run("acme", manual=True)
            assert (
                await ident.store.get_user("acme", users[0].user_id)
            ).unverified_since == first.unverified_since
            assert tokens == 1
            visible = True
            await runner.run("acme", manual=True)
            assert (await ident.store.get_user("acme", users[0].user_id)).unverified_since is None
            clock[0] = 7900
            await runner.run("acme", manual=True)
            assert tokens == 2
            await ident.world.platform.put_secret(
                ident.world.admin, "feishu-app-secret", "new-secret"
            )
            await runner.run("acme", manual=True)
            assert tokens == 3
        audit = await ident.world.platform.store.query_audit("acme", AuditQuery(limit=100))
        assert sum(row.kind == "identity_unverified" for row in audit) == 1


async def test_partial_failure_retries_remaining(identity_world: Any) -> None:
    """第二批失败保留首批停用，下轮立即重试且不跳过后部员工。"""
    async with identity_world() as ident:
        users = await prepared(ident, 51)
        batches: list[list[str]] = []
        fail = True

        def handle(request: httpx.Request) -> httpx.Response:
            """首批成功、尾批临时失败。"""
            if request.method == "POST":
                return token_response()
            ids = request.url.params.get_list("user_ids")
            batches.append(ids)
            if ids == ["u050"] and fail:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"union_id": i, "status": {"is_resigned": True}} for i in ids]
                    },
                },
            )

        async with directory(ident, handle) as runner:
            with pytest.raises(RuntimeUnavailableError):
                await runner.run("acme")
            state = await ident.store.get_reconcile_state("acme")
            assert state.last_run_at is None and state.lease_owner is None
            assert (await ident.store.get_user("acme", users[49].user_id)).status == "disabled"
            assert (await ident.store.get_user("acme", users[50].user_id)).status == "active"
            fail = False
            assert await runner.run("acme") == {"checked": 1, "disabled": 1, "unverified": 0}
            assert [len(batch) for batch in batches] == [50, 1, 1]
        audit = await ident.world.platform.store.query_audit("acme", AuditQuery(limit=100))
        failed = [
            row
            for row in audit
            if row.kind == "identity_reconciled" and row.data.get("result") == "failed"
        ]
        assert len(failed) == 1 and failed[0].data["disabled"] == 50
        metrics = ident.world.platform.metrics.render().decode()
        assert 'xuanpu_reconcile_runs_total{result="failed"} 1.0' in metrics
        assert 'xuanpu_reconcile_users_total{outcome="disabled"} 51.0' in metrics


async def test_two_instances_and_cancellation(identity_world: Any) -> None:
    """共享存储的实例互斥，取消释放租约且没有完成时间。"""
    async with identity_world() as ident:
        await prepared(ident)
        entered, release = asyncio.Event(), asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            """挂住出站请求。"""
            entered.set()
            await release.wait()
            return token_response()

        async with directory(ident, handle) as runner:
            second = IdentityReconcile(
                runner.service,
                control_plane=runner._control,
                vault=runner._vault,
                audit=runner._audit,
                feishu=runner.feishu,
            )
            task = asyncio.create_task(runner.run("acme"))
            await asyncio.wait_for(entered.wait(), 1)
            with pytest.raises(ConflictError):
                await second.run("acme", manual=True)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            state = await ident.store.get_reconcile_state("acme")
            assert state.lease_owner is None and state.last_run_at is None


async def test_lease_loss_before_effect(identity_world: Any) -> None:
    """出站期间租约过期被接手，原执行者不得再停用。"""
    async with identity_world() as ident:
        users = await prepared(ident)
        clock = [1000.0]

        async def handle(request: httpx.Request) -> httpx.Response:
            """模拟暂停超过租约后被另一实例接手。"""
            if request.method == "POST":
                return token_response()
            clock[0] += 121
            assert await ident.store.claim_reconcile(
                "acme", owner="other", now=clock[0], interval=0, lease_seconds=120
            )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [{"union_id": "u000", "status": {"is_resigned": True}}]},
                },
            )

        async with directory(ident, handle) as runner:
            runner._now = lambda: clock[0]
            with pytest.raises(RuntimeUnavailableError):
                await runner.run("acme")
            assert (await ident.store.get_user("acme", users[0].user_id)).status == "active"
            assert (await ident.store.get_reconcile_state("acme")).lease_owner == "other"


async def test_manual_admin_period_and_conflict(identity_world: Any) -> None:
    """手工必须管理员，跳过周期但不跳过租约。"""
    async with identity_world() as ident:
        await prepared(ident)
        key = await ident.system_key()
        member = await ident.system_key(role="tenant_member")

        def handle(request: httpx.Request) -> httpx.Response:
            """返回可见正常员工。"""
            if request.method == "POST":
                return token_response()
            return httpx.Response(
                200, json={"code": 0, "data": {"items": [{"union_id": "u000", "status": {}}]}}
            )

        path = "/v1/identity/feishu/reconcile"
        async with directory(ident, handle) as runner:
            assert (
                await ident.http.post(path, headers={"Authorization": f"Bearer {member}"})
            ).status_code == 403
            for _ in range(2):
                response = await ident.http.post(path, headers={"Authorization": f"Bearer {key}"})
                assert response.status_code == 200
                assert response.json()["checked"] == 1
            with pytest.raises(ConflictError):
                await runner.run("acme")
            assert await ident.store.claim_reconcile(
                "acme", owner="busy", now=runner._now(), interval=0, lease_seconds=120
            )
            assert (
                await ident.http.post(path, headers={"Authorization": f"Bearer {key}"})
            ).status_code == 409


async def test_renewal_allows_long_scan(identity_world: Any) -> None:
    """多个批次跨越初始租约仍能完成，续期延长持有时间。"""
    async with identity_world() as ident:
        await prepared(ident, 101)
        clock = [1000.0]

        def handle(request: httpx.Request) -> httpx.Response:
            """每个批次逻辑上经过一分钟。"""
            if request.method == "POST":
                return token_response()
            clock[0] += 60
            ids = request.url.params.get_list("user_ids")
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": [{"union_id": i, "status": {}} for i in ids]}},
            )

        async with directory(ident, handle) as runner:
            runner._now = lambda: clock[0]
            assert (await runner.run("acme"))["checked"] == 101
            assert clock[0] == 1180


async def test_maintenance_recovery_and_close(
    identity_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """维护同一循环失败后恢复，关闭取消正在对账并释放租约。"""
    async with identity_world() as ident:
        await prepared(ident)
        service = ident.world.platform.identity
        maintenance = service.maintenance
        await maintenance.close()
        maintenance._interval = 0.01
        entered = asyncio.Event()
        requests = 0

        async def handle(request: httpx.Request) -> httpx.Response:
            """先失败再挂住重试，等待关闭。"""
            nonlocal requests
            requests += 1
            if requests == 1:
                return httpx.Response(503)
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("不可到达")

        async with directory(ident, handle):
            service.start()
            task = maintenance._task
            service.start()
            assert task is maintenance._task
            await asyncio.wait_for(entered.wait(), 1)
            assert requests == 2
            close_disabling = service.disabling.close
            async def checked_close() -> None:
                """平台关闭时须先停对账，避免会话收尾期间新增停用。"""
                assert not maintenance.running
                state = await ident.store.get_reconcile_state("acme")
                assert state.lease_owner is None
                await close_disabling()
            monkeypatch.setattr(service.disabling, "close", checked_close)
            await ident.world.platform.close()
            assert not maintenance.running
            state = await ident.store.get_reconcile_state("acme")
            assert state.lease_owner is None and state.last_run_at is None


async def test_unbound_and_inactive_skipped(identity_world: Any) -> None:
    """未绑定配置不会触发飞书出站。"""
    async with identity_world() as ident:
        await ident.configure_feishu()

        def handle(request: httpx.Request) -> httpx.Response:
            """未绑定租户不可出站。"""
            raise AssertionError("不应调用飞书")

        async with directory(ident, handle) as runner:
            await runner.run_due()
            assert await ident.store.get_reconcile_state("acme") is None


async def test_step_timeout_releases_lease(
    identity_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单步超时中断运行释放租约，下轮仍可认领。"""
    async with identity_world() as ident:
        await prepared(ident)
        monkeypatch.setattr("xuanpu_server.identity.reconcile.STEP_SECONDS", 0.01)

        async def handle(request: httpx.Request) -> httpx.Response:
            """永不返回的飞书请求必须可取消。"""
            await asyncio.Event().wait()
            raise AssertionError("不可到达")

        async with directory(ident, handle) as runner:
            with pytest.raises(RuntimeUnavailableError):
                await runner.run("acme")
            state = await ident.store.get_reconcile_state("acme")
            assert state.lease_owner is None and state.last_run_at is None


async def test_cancel_cleanup_is_bounded(
    identity_world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败审计和释放均挂起时关闭仍有界，保留取消异常。"""
    async with identity_world() as ident:
        await prepared(ident)
        entered = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            """阻塞飞书直到取消。"""
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("不可到达")

        async def stuck(*args: Any, **kwargs: Any) -> None:
            """模拟不可达存储。"""
            await asyncio.Event().wait()

        async with directory(ident, handle) as runner:
            monkeypatch.setattr("xuanpu_server.identity.reconcile.STEP_SECONDS", 0.02)
            task = asyncio.create_task(runner.run("acme"))
            await entered.wait()
            monkeypatch.setattr(runner._audit, "control", stuck)
            monkeypatch.setattr(runner.store, "release_reconcile", stuck)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.3)


async def test_preflight_timeout(identity_world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """前置查配置挂起也有界。"""
    async with identity_world() as ident:
        await prepared(ident)
        runner = ident.world.platform.identity.reconcile

        async def stuck(*args: Any, **kwargs: Any) -> None:
            """模拟不可达存储。"""
            await asyncio.Event().wait()

        monkeypatch.setattr("xuanpu_server.identity.reconcile.STEP_SECONDS", 0.01)
        monkeypatch.setattr(runner.store, "get_identity_config", stuck)
        key = await ident.system_key()
        response = await asyncio.wait_for(
            ident.http.post('/v1/identity/feishu/reconcile', headers={'Authorization': f'Bearer {key}'}), 0.2,
        )
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'runtime_unavailable' 


@pytest.mark.parametrize('lost', [False, True])
async def test_finish_failure_has_no_success_audit(identity_world: Any, monkeypatch: pytest.MonkeyPatch, lost: bool) -> None:
    """完成写库失败或已失租，只记失败终态。"""
    async with identity_world() as ident:
        await prepared(ident)
        async def finish(*args: Any, **kwargs: Any) -> bool:
            """模拟完成时数据库异常或租约已移交。"""
            if lost:
                return False
            raise OSError('数据库不可达')
        def handle(request: httpx.Request) -> httpx.Response:
            """通讯录查询正常。"""
            if request.method == 'POST':
                return token_response()
            return httpx.Response(200,json={'code':0,'data':{'items':[]}})
        async with directory(ident, handle) as runner:
            monkeypatch.setattr(runner.store,'finish_reconcile',finish)
            with pytest.raises(RuntimeUnavailableError if lost else OSError):
                await runner.run('acme')
            audit = await ident.world.platform.store.query_audit('acme',AuditQuery(limit=100))
            results = [row.data['result'] for row in audit if row.kind == 'identity_reconciled']
            assert results == ['failed']


async def test_completed_audit_failure_does_not_rewrite_result(identity_world: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """完成后的审计故障不撤销完成时间，不补记失败指标和审计。"""
    async with identity_world() as ident:
        await prepared(ident)
        def handle(request: httpx.Request) -> httpx.Response:
            """返回可见正常员工，不触发额外审计。"""
            if request.method == 'POST':
                return token_response()
            return httpx.Response(200,json={'code':0,'data':{'items':[{'union_id':'u000','status':{}}]}})
        async with directory(ident,handle) as runner:
            attempts = []
            async def fail_audit(*args: Any, **kwargs: Any) -> None:
                """只记录尝试的结果，然后模拟审计失败。"""
                attempts.append(args[3]['result'])
                raise OSError('审计不可达')
            monkeypatch.setattr(runner._audit,'control',fail_audit)
            with pytest.raises(OSError):
                await runner.run('acme')
            assert attempts == ['succeeded']
            assert (await runner.store.get_reconcile_state('acme')).last_run_at is not None
            metrics = ident.world.platform.metrics.render().decode()
            assert 'xuanpu_reconcile_runs_total{result="succeeded"} 1.0' in metrics
            assert 'xuanpu_reconcile_runs_total{result="failed"} 0.0' in metrics

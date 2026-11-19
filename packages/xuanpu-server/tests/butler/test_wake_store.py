"""唤醒事件与待处理项存储契约：去重、决定先写者为准、确认幂等、隔离、版本。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from xuanpu_server.butler.wake_models import ButlerEvent, PendingItem, WakeDecision
from xuanpu_server.errors import ConflictError, NotFoundError

from .test_dispatch_store import ALICE, BOB, GLOBEX_ALICE, LIMITS, _record, store  # noqa: F401

if TYPE_CHECKING:
    from xuanpu_server.butler.dispatch_store import DispatchStore

T0 = datetime(2026, 11, 14, 8, tzinfo=UTC)


def _wake(dispatches: DispatchStore):  # type: ignore[no-untyped-def]
    """与派工存储同后端的唤醒存储。"""
    from xuanpu_server.butler.memory import MemoryDispatchStore
    from xuanpu_server.butler.wake_postgres import PostgresWakeStore
    from xuanpu_server.butler.wake_store import MemoryWakeStore

    if isinstance(dispatches, MemoryDispatchStore):
        return MemoryWakeStore()
    return PostgresWakeStore(dispatches._pool, namespace=_namespace(dispatches))  # noqa: SLF001


def _namespace(dispatches: object):  # type: ignore[no-untyped-def]
    """从派工存储的表名还原命名空间。"""
    from taifeng_stores import SqlNamespace

    schema, table = dispatches._table.replace('"', "").split(".")  # type: ignore[attr-defined]  # noqa: SLF001
    return SqlNamespace(schema=schema, prefix=table.removesuffix("butler_dispatch"))


def _event(event_id: str, key: str, ms: int = 0) -> ButlerEvent:
    """Alice 派工 dsp_1 上的一条事件。"""
    return ButlerEvent(event_id=event_id, tenant_id="acme", user_id="usr_alice",
                       dispatch_id="dsp_1", key=key, kind="finished",
                       created_at=T0 + timedelta(milliseconds=ms))


def _item(pending_id: str, key: str, user: str = "usr_alice") -> PendingItem:
    """dsp_1 上的问题项。"""
    return PendingItem(pending_id=pending_id, tenant_id="acme", user_id=user, dispatch_id="dsp_1",
                       source_key=key, kind="question", question="客户编号？", host="session",
                       host_ref="ses_1", created_at=T0, updated_at=T0)


async def test_events_dedupe_decide_ack(store: DispatchStore) -> None:  # noqa: F811
    """同键去重；决定先写者为准；确认后不再出现在未确认列表；重复确认无副作用。"""
    await store.create(ALICE, _record(), **LIMITS)
    wake = _wake(store)
    first, created = await wake.emit(_event("bev_1", "round:1"))
    assert created
    again, created = await wake.emit(_event("bev_2", "round:1", 5))
    assert not created and again.event_id == "bev_1"
    await wake.emit(_event("bev_3", "round:2", 10))
    assert [e.event_id for e in await wake.unacked("acme")] == ["bev_1", "bev_3"]
    assert await wake.unacked("globex") == ()
    deliver = WakeDecision(action="deliver", passed=True)
    ask = WakeDecision(action="ask_user", question="?")
    assert (await wake.decide("acme", "bev_1", deliver)).decision == deliver
    assert (await wake.decide("acme", "bev_1", ask)).decision == deliver
    await wake.ack("acme", "bev_1", T0)
    await wake.ack("acme", "bev_1", T0)
    assert [e.event_id for e in await wake.unacked("acme")] == ["bev_3"]
    with pytest.raises(NotFoundError):
        await wake.ack("globex", "bev_3", T0)


async def test_pending_isolation_and_versions(store: DispatchStore) -> None:  # noqa: F811
    """同键去重；他人与他租户 404；版本不符冲突；状态过滤。"""
    await store.create(ALICE, _record(), **LIMITS)
    wake = _wake(store)
    item, created = await wake.open_pending(_item("pnd_1", "ask:1"))
    assert created
    same, created = await wake.open_pending(_item("pnd_2", "ask:1"))
    assert not created and same.pending_id == "pnd_1"
    for other in (BOB, GLOBEX_ALICE):
        with pytest.raises(NotFoundError):
            await wake.get_pending(other, "pnd_1")
        assert await wake.list_pending(other) == ()
    answered = await wake.update_pending(ALICE, "pnd_1", expected_version=1, at=T0,
                                         state="answered", answer="C-1024")
    assert answered.version == 2 and answered.answer == "C-1024"
    with pytest.raises(ConflictError):
        await wake.update_pending(ALICE, "pnd_1", expected_version=1, at=T0, state="resolved")
    assert await wake.list_pending(ALICE) == ()
    assert len(await wake.list_pending(ALICE, open_only=False, dispatch_id="dsp_1")) == 1


async def test_events_for_dispatch_in_order_and_owner_only(store: DispatchStore) -> None:  # noqa: F811
    """一次派工的事件按创建先后；他人与别的租户读不到（R7-3）。"""
    await store.create(ALICE, _record(), **LIMITS)
    wake = _wake(store)
    await wake.emit(_event("bev_2", "round:2", 10))
    await wake.emit(_event("bev_1", "round:1"))
    assert [e.event_id for e in await wake.events_for_dispatch(ALICE, "dsp_1")] == ["bev_1", "bev_2"]
    assert await wake.events_for_dispatch(BOB, "dsp_1") == ()
    assert await wake.events_for_dispatch(GLOBEX_ALICE, "dsp_1") == ()
    assert await wake.events_for_dispatch(ALICE, "dsp_x") == ()

"""Regression tests for TurnStream overflow termination, runtime eviction races, and lock GC."""

from __future__ import annotations

import asyncio
import gc
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.controlplane import InMemoryControlPlane
from server.core.controlplane.models import SessionRecord
from server.core.tenancy import Principal
from server.engine.runtime.assembler import RuntimeAssembler, RuntimeDefaults
from server.engine.runtime.events import _STREAM_BUFFER, TurnStream
from server.engine.runtime.sessions import SessionManager, _LiveSession


def _delta_event(seq: int) -> Any:
    return SimpleNamespace(
        submission_id="sub_overflow",
        seq=seq,
        timestamp=datetime.now(UTC),
        msg=SimpleNamespace(kind="assistant_text", data={"delta": str(seq)}),
    )


async def test_turn_stream_events_terminates_after_buffer_overflow() -> None:
    """An overflowed TurnStream must exit after draining buffered items rather than hanging forever."""
    stream = TurnStream("sub_overflow")
    for seq in range(_STREAM_BUFFER + 5):
        stream.push(_delta_event(seq))
    assert stream.overflowed is True

    received: list[int] = []
    async with asyncio.timeout(2.0):
        async for event in stream.events():
            received.append(event.seq)

    assert len(received) == _STREAM_BUFFER
    assert received[0] == 0
    assert received[-1] == _STREAM_BUFFER - 1


def _fake_runtime(
    key: tuple[str, str, str, str],
    *,
    last_used: float,
    fail_close: bool = False,
) -> Any:
    closed = {"done": False}

    async def close() -> None:
        if fail_close:
            raise RuntimeError("cannot reap")
        closed["done"] = True

    return SimpleNamespace(
        key=key,
        last_used=last_used,
        closing=False,
        retainers=0,
        live_sessions=lambda: 0,
        close=close,
        closed=closed,
    )


def _bare_assembler(*, now_box: list[float]) -> RuntimeAssembler:
    return RuntimeAssembler(
        data_dir=None,  # type: ignore[arg-type]
        models=None,  # type: ignore[arg-type]
        sandboxes=None,  # type: ignore[arg-type]
        states=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        audit=None,  # type: ignore[arg-type]
        submissions=None,  # type: ignore[arg-type]
        defaults=RuntimeDefaults(runtime_idle_seconds=60.0),
        now=lambda: now_box[0],
    )


async def test_evict_idle_rechecks_last_used_under_lock() -> None:
    """If get() refreshes runtime.last_used while evict_idle() waits for the lock, do not evict."""
    now = [1000.0]
    assembler = _bare_assembler(now_box=now)
    key = ("acme", "pkg", "1.0.0", "scope-1")
    runtime = _fake_runtime(key, last_used=100.0)
    assembler._runtimes[key] = runtime

    lock = assembler._lock_for(key)
    await lock.acquire()
    evict_task = asyncio.create_task(assembler.evict_idle())
    await asyncio.sleep(0)  # evict_idle passed outer check and is now waiting on lock

    # Simulate concurrent get() refreshing last_used before releasing lock
    runtime.last_used = now[0]
    lock.release()

    evicted = await evict_task
    assert evicted == []
    assert runtime.closing is False
    assert runtime.closed["done"] is False
    assert assembler._runtimes[key] is runtime


async def test_evict_idle_continues_past_single_close_failure_and_propagates_error() -> None:
    """One runtime failing to close must not prevent other idle runtimes from being evicted."""
    now = [1000.0]
    assembler = _bare_assembler(now_box=now)
    bad_key = ("acme", "pkg", "1.0.0", "scope-bad")
    good_key = ("acme", "pkg", "1.0.0", "scope-good")
    bad_rt = _fake_runtime(bad_key, last_used=100.0, fail_close=True)
    good_rt = _fake_runtime(good_key, last_used=100.0, fail_close=False)
    assembler._runtimes[bad_key] = bad_rt
    assembler._runtimes[good_key] = good_rt

    with pytest.raises(RuntimeError, match="cannot reap"):
        await assembler.evict_idle()

    # The failing runtime stays registered and marked closing, while the healthy one is evicted
    assert bad_key in assembler._runtimes
    assert bad_rt.closing is True
    assert good_key not in assembler._runtimes
    assert good_rt.closed["done"] is True


async def test_assembler_locks_are_garbage_collected_when_unused() -> None:
    """Per-scope locks use WeakValueDictionary and do not accumulate unboundedly."""
    assembler = _bare_assembler(now_box=[1000.0])
    key = ("acme", "pkg", "1.0.0", "ephemeral-scope")
    async with assembler._lock_for(key):
        assert key in assembler._locks
    gc.collect()
    assert key not in assembler._locks


def _bare_sessions(store: InMemoryControlPlane, *, now_box: list[float]) -> SessionManager:
    async def noop_audit(*args: Any, **kwargs: Any) -> None:
        return None

    return SessionManager(
        store=store,
        users=None,  # type: ignore[arg-type]
        packages=None,  # type: ignore[arg-type]
        runtimes=None,  # type: ignore[arg-type]
        quota=None,  # type: ignore[arg-type]
        audit=SimpleNamespace(control=noop_audit),  # type: ignore[arg-type]
        metrics=SimpleNamespace(backfill_failed=lambda: None),  # type: ignore[arg-type]
        submissions=None,  # type: ignore[arg-type]
        instance_id="inst-1",
        now=lambda: now_box[0],
    )


async def test_session_open_locked_refreshes_last_used_and_close_updates_under_lock() -> None:
    """_open_locked refreshes last_used on existing sessions; close updates DB status under lock."""
    now = [500.0]
    store = InMemoryControlPlane()
    manager = _bare_sessions(store, now_box=now)

    record = SessionRecord(
        tenant_id="acme",
        session_id="ses_1",
        package="pkg",
        version="1.0.0",
        entry="main",
        status="active",
        created_at=100.0,
        updated_at=100.0,
        created_by="key:k1",
    )
    await store.create_session(record)
    key = ("acme", "ses_1")

    live = _LiveSession(
        record=record,
        runtime=None,  # type: ignore[arg-type]
        engine=None,  # type: ignore[arg-type]
        hub=None,  # type: ignore[arg-type]
        last_used=100.0,
    )
    manager._live[key] = live

    now[0] = 900.0
    async with manager._lock_for(key):
        reopened = await manager._open_locked(record)
    assert reopened is live
    assert live.last_used == 900.0

    # Verify close() updates status while still holding _lock_for(key)
    lock_held_during_update: bool | None = None
    orig_update = store.update_session

    async def tracked_update(*args: Any, **kwargs: Any) -> SessionRecord:
        nonlocal lock_held_during_update
        lock_held_during_update = manager._lock_for(key).locked()
        return await orig_update(*args, **kwargs)

    async def noop_detach(_key: Any, *, force: bool) -> bool:
        manager._live.pop(_key, None)
        return True

    store.update_session = tracked_update  # type: ignore[method-assign]
    manager._detach = noop_detach  # type: ignore[method-assign]

    admin = Principal(role="tenant_admin", tenant_id="acme", key_id="k1", user_id=None)
    closed = await manager.close(admin, "ses_1")
    assert closed.status == "closed"
    assert lock_held_during_update is True

    # Unused per-session lock is collected once no coroutine holds it
    gc.collect()
    assert key not in manager._locks

"""Hot-path read optimizations: lock-free reads in KnowledgeService, ManagedHttpTools, and WorkspaceService.download."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from server.core.objectstore.local import LocalObjectStore
from server.core.tenancy import Principal
from server.domains.connections.managed_http import ManagedHttpTools
from server.domains.connections.memory import InMemoryConnectionStore
from server.domains.connections.package_bindings import binding_key
from server.domains.knowledge.context_store import CreateContext, MemoryContextStore, PinInput
from server.domains.knowledge.contexts import KnowledgeContexts
from server.domains.knowledge.service import KnowledgeConfig, KnowledgeService, RegisterSource
from server.domains.knowledge.store import MemoryKnowledgeStore
from server.domains.workspace.memory import InMemoryWorkspaceStore
from server.domains.workspace.service import WorkspaceService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def _alice() -> Principal:
    return Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id="usr_alice")


class _StubIdentity:
    """Minimal active-tenant/active-employee stub."""

    def __init__(self, *, active: bool = True) -> None:
        self.active = active

    async def get_user(self, tenant_id: str, user_id: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            tenant_id=tenant_id,
            user_id=user_id,
            role="tenant_member",
            status="active" if self.active else "disabled",
        )


class _StubControl:
    """Minimal active-tenant stub."""

    async def get_tenant(self, tenant_id: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(tenant_id=tenant_id, status="active")


async def test_knowledge_readable_and_listing_avoid_workspace_write_transactions(
    tmp_path: Path,
) -> None:
    """KnowledgeService._readable() and KnowledgeContexts.listing() use lock-free file_source."""
    store = InMemoryWorkspaceStore(audit=lambda _records: None)
    objects = LocalObjectStore(tmp_path / "objects")
    workspace = WorkspaceService(
        store=store,
        control=_StubControl(),  # type: ignore[arg-type]
        identity=_StubIdentity(),  # type: ignore[arg-type]
        objects=objects,
    )
    alice = _alice()

    uploaded = await workspace.upload(
        alice, "handbook.txt", b"policy alpha\npolicy beta\n", "text/plain", "req-upload-1"
    )
    knowledge = KnowledgeService(
        workspace,
        MemoryKnowledgeStore(),
        config=KnowledgeConfig(enabled=True, tenants=("acme",)),
        now=lambda: 1_700_000_000.0,
    )
    registered = await knowledge.register(
        alice, RegisterSource(kind="workspace_file", item_id=uploaded["id"], revision=1)
    )
    source_id = str(registered["id"])

    contexts = KnowledgeContexts(knowledge, MemoryContextStore(), sampler=lambda *a, **kw: None)  # type: ignore[arg-type]
    await contexts.create(
        alice,
        CreateContext(request_id="ctx-1", pins=[PinInput(source_id=source_id, ordinal=0)]),
    )
    await contexts.create(
        alice,
        CreateContext(request_id="ctx-2", pins=[PinInput(source_id=source_id, ordinal=0)]),
    )

    tx_calls = 0
    file_source_calls = 0
    orig_tx = store.transaction
    orig_file_source = store.file_source

    @asynccontextmanager
    async def counted_tx(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        nonlocal tx_calls
        tx_calls += 1
        async with orig_tx(*args, **kwargs) as state:
            yield state

    async def counted_file_source(*args: Any, **kwargs: Any) -> Any:
        nonlocal file_source_calls
        file_source_calls += 1
        return await orig_file_source(*args, **kwargs)

    store.transaction = counted_tx  # type: ignore[method-assign]
    store.file_source = counted_file_source  # type: ignore[method-assign]

    listed_sources = await knowledge.sources(alice)
    assert listed_sources["total"] == 1
    assert tx_calls == 0
    assert file_source_calls == 1

    # listing() across 2 contexts pinning the same source deduplicates readability checks
    file_source_calls = 0
    listed_contexts = await contexts.listing(alice)
    assert listed_contexts["total"] == 2
    assert tx_calls == 0
    assert file_source_calls == 1

    # Uploading a new revision reports has_update=True via file_source without any tx
    store.transaction = orig_tx  # type: ignore[method-assign]
    await workspace.replace_content(
        alice, uploaded["id"], b"policy v2\n", "text/plain", int(uploaded["version"]), "req-upload-2"
    )
    store.transaction = counted_tx  # type: ignore[method-assign]
    tx_calls = 0
    updated_sources = await knowledge.sources(alice)
    assert tx_calls == 0
    sources_list = updated_sources["sources"]
    assert isinstance(sources_list, list) and sources_list[0]["has_update"] is True  # type: ignore[index]

    # Trashing the file marks the source stale via file_source without any tx
    store.transaction = orig_tx  # type: ignore[method-assign]
    await workspace.trash(alice, uploaded["id"], int(uploaded["version"]) + 1)
    store.transaction = counted_tx  # type: ignore[method-assign]
    tx_calls = 0
    stale_sources = await knowledge.sources(alice)
    assert tx_calls == 0
    stale_list = stale_sources["sources"]
    assert isinstance(stale_list, list) and stale_list[0]["status"] == "stale"  # type: ignore[index]


async def test_managed_http_lookup_uses_lock_free_read_record() -> None:
    """ManagedHttpTools.lookup() reads via store.read_record() instead of opening a tenant FOR UPDATE tx."""
    conn_store = InMemoryConnectionStore()
    digest = hashlib.sha256(b"pkg").hexdigest()
    key = binding_key(digest, "wecom", "getProfile")
    async with conn_store.transaction("acme") as state:
        state.records[key] = {
            "installation_id": "inst_1",
            "operation": "profile.read",
            "endpoint": "/cgi-bin/user/get",
        }

    tx_calls = 0
    read_calls = 0
    orig_tx = conn_store.transaction
    orig_read = conn_store.read_record

    @asynccontextmanager
    async def counted_tx(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        nonlocal tx_calls
        tx_calls += 1
        async with orig_tx(*args, **kwargs) as state:
            yield state

    async def counted_read(*args: Any, **kwargs: Any) -> Any:
        nonlocal read_calls
        read_calls += 1
        return await orig_read(*args, **kwargs)

    conn_store.transaction = counted_tx  # type: ignore[method-assign]
    conn_store.read_record = counted_read  # type: ignore[method-assign]

    from types import SimpleNamespace

    broker = SimpleNamespace(service=SimpleNamespace(store=conn_store))
    tools = ManagedHttpTools(broker, None, None)  # type: ignore[arg-type]

    found = await tools.lookup("acme", digest, "wecom", "getProfile")
    assert found is not None and found["installation_id"] == "inst_1"
    assert tx_calls == 0
    assert read_calls == 1

    missing = await tools.lookup("acme", digest, "wecom", "missingOp")
    assert missing is None
    assert tx_calls == 0
    assert read_calls == 2


async def test_workspace_download_releases_transaction_before_object_io(tmp_path: Path) -> None:
    """WorkspaceService.download() must exit the DB transaction before awaiting object storage."""
    store = InMemoryWorkspaceStore(audit=lambda _records: None)
    objects = LocalObjectStore(tmp_path / "objects")
    workspace = WorkspaceService(
        store=store,
        control=_StubControl(),  # type: ignore[arg-type]
        identity=_StubIdentity(),  # type: ignore[arg-type]
        objects=objects,
    )
    alice = _alice()

    uploaded = await workspace.upload(
        alice, "report.txt", b"quarterly numbers", "text/plain", "req-download-1"
    )

    in_tx = False
    tx_active_during_get: bool | None = None
    orig_tx = store.transaction
    orig_get = objects.get_limited

    @asynccontextmanager
    async def tracking_tx(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        nonlocal in_tx
        in_tx = True
        try:
            async with orig_tx(*args, **kwargs) as state:
                yield state
        finally:
            in_tx = False

    async def tracking_get(*args: Any, **kwargs: Any) -> bytes:
        nonlocal tx_active_during_get
        tx_active_during_get = in_tx
        return await orig_get(*args, **kwargs)

    store.transaction = tracking_tx  # type: ignore[method-assign]
    objects.get_limited = tracking_get  # type: ignore[method-assign]

    content, mime, name = await workspace.download(alice, uploaded["id"])
    assert content == b"quarterly numbers"
    assert mime == "text/plain"
    assert name == "report.txt"
    assert tx_active_during_get is False

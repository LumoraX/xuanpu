"""模型路由：档位选择与模型名钉死。"""

from __future__ import annotations

from typing import Any

import pytest
import taifeng
from xuanpu_server.errors import RuntimeUnavailableError, SecretMissingError
from xuanpu_server.runtime.models import (
    GatewayConfig,
    GatewayModelClientFactory,
    ModelProfile,
    TierBinding,
    TieredModelClient,
)

PROFILES = {
    "fast": ModelProfile("small-model"),
    "balanced": ModelProfile("medium-model"),
    "deep": ModelProfile("large-model"),
}


class RecordingSession:
    """记下收到的请求。"""

    def __init__(self, owner: RecordingClient) -> None:
        self._owner = owner

    async def stream(self, request: Any) -> Any:
        self._owner.requests.append(request)
        yield taifeng.ResponseEvent(kind="created")

    async def __aenter__(self) -> RecordingSession:
        self._owner.entered += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._owner.exited += 1


class RecordingClient:
    """记下会话创建参数与请求的模型客户端。"""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.session_models: list[str | None] = []
        self.entered = 0
        self.exited = 0

    def session(self, *, cancel: taifeng.CancellationToken, model: str | None = None) -> Any:
        self.session_models.append(model)
        return RecordingSession(self)


def make_client(default: str = "balanced") -> tuple[TieredModelClient, dict[str, RecordingClient]]:
    """三个档位各一个记录客户端。"""
    inner = {tier: RecordingClient() for tier in PROFILES}
    tiers = {
        tier: TierBinding(client=inner[tier], model=profile.model)
        for tier, profile in PROFILES.items()
    }
    return TieredModelClient(tiers, default_tier=default), inner


async def run_once(client: TieredModelClient, skill_model: str | None) -> None:
    """按内核的用法走一遍：建会话、进入、发请求、退出。"""
    request = taifeng.ApiRequest(model=skill_model or "", messages=[])
    async with client.session(cancel=taifeng.CancellationToken(), model=skill_model) as session:
        async for _ in session.stream(request):
            pass


@pytest.mark.parametrize(
    ("skill_model", "expected_tier"),
    [
        (None, "balanced"),
        ("fast", "fast"),
        ("deep", "deep"),
        ("gpt-4o", "balanced"),
        ("FAST", "balanced"),
        ("", "balanced"),
    ],
)
async def test_tier_selection_and_model_pinning(
    skill_model: str | None, expected_tier: str
) -> None:
    """skill 写的是档位名就换档；写了别的就用业务包的档位。发出去的模型名总是平台配置的。"""
    client, inner = make_client()
    await run_once(client, skill_model)
    used = inner[expected_tier]
    assert [request.model for request in used.requests] == [PROFILES[expected_tier].model]
    # skill 写的模型名不往下传
    assert used.session_models == [None]
    assert (used.entered, used.exited) == (1, 1)
    for tier, other in inner.items():
        if tier != expected_tier:
            assert other.requests == []


async def test_original_request_is_not_mutated() -> None:
    """改写模型名用的是副本，内核手里的请求不变。"""
    client, _ = make_client()
    request = taifeng.ApiRequest(model="written-by-skill", messages=[])
    async with client.session(cancel=taifeng.CancellationToken()) as session:
        async for _ in session.stream(request):
            pass
    assert request.model == "written-by-skill"


def test_default_tier_must_exist() -> None:
    """默认档位必须有对应的客户端。"""
    with pytest.raises(ValueError, match="默认档位"):
        TieredModelClient({"fast": TierBinding(RecordingClient(), "m")}, default_tier="deep")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"profiles": {"fast": ModelProfile("m")}, "api_key": "k"},
        {"profiles": PROFILES},
        {"profiles": PROFILES, "api_key": "k", "api_key_secret": "s"},
    ],
)
def test_gateway_config_validation(kwargs: dict[str, Any]) -> None:
    """三个档位要配齐；凭据来源必须且只能有一个。"""
    with pytest.raises(ValueError, match="档位|只能给一个"):
        GatewayConfig(base_url="http://gateway/v1", **kwargs)


async def test_factory_uses_shared_or_per_tenant_credentials(world: Any) -> None:
    """平台共用凭据时不查保管库；按租户取凭据时从保管库拿，没配就说清楚缺什么。"""
    shared = GatewayModelClientFactory(
        GatewayConfig(base_url="http://gateway/v1", profiles=PROFILES, api_key="platform-key"),
        world.platform.vault,
    )
    assert isinstance(await shared.create("acme", "fast"), TieredModelClient)
    with pytest.raises(RuntimeUnavailableError, match="档位"):
        await shared.create("acme", "huge")

    per_tenant = GatewayModelClientFactory(
        GatewayConfig(base_url="http://gateway/v1", profiles=PROFILES, api_key_secret="llm-key"),
        world.platform.vault,
    )
    with pytest.raises(SecretMissingError, match="llm-key"):
        await per_tenant.create("acme", "balanced")
    await world.platform.put_secret(world.admin, "llm-key", "tenant-key")
    assert isinstance(await per_tenant.create("acme", "balanced"), TieredModelClient)

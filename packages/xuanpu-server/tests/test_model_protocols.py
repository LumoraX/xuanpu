"""模型协议配置、能力和真实客户端线协议的回归。"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import taifeng
from xuanpu_server.bootstrap import open_platform
from xuanpu_server.config import ConfigError, ModelsConfig, parse_config
from xuanpu_server.runtime.models import GatewayConfig, GatewayModelClientFactory, ModelProfile

if TYPE_CHECKING:
    from pathlib import Path


def settings(data_dir: str, protocol: str = "chat", state: str = "local") -> str:
    """合法测试配置，仅使用无效的本机测试凭据。"""
    return json.dumps(
        {
            "data_dir": data_dir,
            "root_key_hash": "a" * 64,
            "master_keys": {"k": base64.b64encode(b"k" * 32).decode()},
            "primary_master_key": "k",
            "models": {
                "base_url": "https://gateway.invalid/v1",
                "api_key": "test-key",
                "protocol": protocol,
                "tiers": {
                    tier: {"model": tier + "-model"} for tier in ("fast", "balanced", "deep")
                },
            },
            "state": {
                "backend": state,
                **(
                    {"postgres": {"dsn": "postgresql://u:p@localhost/test"}}
                    if state == "postgres"
                    else {}
                ),
            },
        }
    )


def test_protocol_default_is_chat() -> None:
    """旧配置默认使用 Chat，不改变既有部署。"""
    config = ModelsConfig.model_validate(
        {
            "base_url": "https://gateway.invalid/v1",
            "api_key": "test-key",
            "tiers": {tier: {"model": "m"} for tier in ("fast", "balanced", "deep")},
        }
    )
    assert config.protocol == "chat"


@pytest.mark.parametrize("protocol", ["responses", "codex_responses"])
def test_postgres_state_rejects_responses(tmp_path: Path, protocol: str) -> None:
    """外部消息主存尚不支持 Responses，配置解析时给明确原因。"""
    with pytest.raises(ConfigError, match="state.*postgres.*chat"):
        parse_config(settings(str(tmp_path), protocol, "postgres"), variables={})
    data = json.loads(settings(str(tmp_path), protocol))
    data["control_plane"] = {
        "backend": "postgres",
        "postgres": {"dsn": "postgresql://u:p@localhost/test"},
    }
    assert parse_config(json.dumps(data), variables={}).models.protocol == protocol


def test_unknown_protocol_rejected(tmp_path: Path) -> None:
    """不猜测协议或静默回退。"""
    with pytest.raises(ConfigError, match="protocol"):
        parse_config(settings(str(tmp_path), "unknown"), variables={})


@pytest.mark.parametrize(
    "protocol,client_name",
    [
        ("chat", "OpenAIChatClient"),
        ("responses", "OpenAIResponsesClient"),
        ("codex_responses", "CodexResponsesClient"),
    ],
)
async def test_factory_protocol_and_capabilities(
    world: Any, protocol: str, client_name: str
) -> None:
    """所有档位使用指定公开客户端，并原样传递能力给内核。"""
    profiles = {tier: ModelProfile(tier + "-model") for tier in ("fast", "balanced", "deep")}
    config = GatewayConfig(
        base_url="https://gateway.invalid/v1",
        profiles=profiles,
        api_key="test-key",
        protocol=protocol,
    )
    client = await GatewayModelClientFactory(config, world.platform.vault).create(
        "acme", "balanced"
    )
    expected = getattr(taifeng, client_name)
    assert client.capabilities == expected.capabilities
    assert all(isinstance(binding.client, expected) for binding in client._tiers.values())


@pytest.mark.parametrize("protocol", ["responses", "codex_responses"])
async def test_bootstrap_passes_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    """正式装配不能丢弃配置的协议字段。"""
    captured = []

    def factory(config: GatewayConfig, vault: Any) -> GatewayModelClientFactory:
        """记录装配参数后仍创建真实工厂。"""
        captured.append(config)
        return GatewayModelClientFactory(config, vault)

    monkeypatch.setattr("xuanpu_server.bootstrap.GatewayModelClientFactory", factory)
    config = parse_config(settings(str(tmp_path), protocol), variables={})
    async with open_platform(config):
        assert captured[0].protocol == protocol


@pytest.mark.parametrize("protocol", ["responses", "codex_responses"])
async def test_responses_wire(world: Any, monkeypatch: pytest.MonkeyPatch, protocol: str) -> None:
    """真实公开客户端发 Responses 流请求，平台只固定模型名。"""
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        """模拟最小 completed 响应，保留实际 HTTP 载荷供断言。"""
        requests.append(request)
        item = {
            "type": "message",
            "id": "msg_test",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "完成", "annotations": []}],
        }
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "status": "in_progress", "content": []},
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_test",
                    "status": "completed",
                    "output": [item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            },
        ]
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events),
        )

    original = httpx.AsyncClient

    def transport_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        """只替换网络传输，仍运行真实客户端解析。"""
        return original(*args, **kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", transport_client)
    profiles = {tier: ModelProfile(tier + "-model") for tier in ("fast", "balanced", "deep")}
    config = GatewayConfig(
        base_url="https://gateway.invalid/v1",
        profiles=profiles,
        api_key="test-key",
        protocol=protocol,
    )
    client = await GatewayModelClientFactory(config, world.platform.vault).create(
        "acme", "balanced"
    )
    request = taifeng.ApiRequest.model_validate(
        {
            "model": "skill-cannot-override",
            "system_prompt": ["测试指令"],
            "input_items": [{"type": "message", "role": "user", "content": "测试输入"}],
        }
    )
    async with client.session(cancel=taifeng.CancellationToken(), model="deep") as session:
        events = [event async for event in session.stream(request)]
    assert any(event.kind == "completed" for event in events)
    assert requests[0].url.path == "/v1/responses"
    body = json.loads(requests[0].content)
    assert body["model"] == "deep-model"
    assert body["stream"] is True and body["store"] is False
    assert isinstance(body["input"], list)
    assert request.model == "skill-cannot-override"
    if protocol == "codex_responses":
        assert body["instructions"] == "测试指令"

"""模型路由：把业务包声明的档位换算成实际的模型客户端。

业务方只选档位（``fast`` / ``balanced`` / ``deep``），不写模型名、不碰密钥（ADR 0001 后果）。
平台按部署配置把档位对应到模型网关上的模型；网关负责各家模型的接入、限流与回退
（ADR 0003，采用兼容 OpenAI Chat 或 Responses 的网关，不自建协议适配器）。

skill 自己的 ``model`` 字段只在取值是档位名时生效，用来给个别 skill 换档；写了具体模型名
则忽略并沿用业务包的档位——模型名是部署细节，不该出现在业务包里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args, runtime_checkable

import taifeng
from xuanpu_spec.manifest import ModelTier

from xuanpu_server.errors import RuntimeUnavailableError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from xuanpu_server.secrets import SecretVault

TIERS: tuple[str, ...] = get_args(ModelTier)


@dataclass(frozen=True)
class ModelProfile:
    """一个档位对应的模型。

    Attributes:
        model: 网关上的模型名。
        timeout_seconds: 单次调用的超时。
    """

    model: str
    timeout_seconds: float = 300.0


@dataclass(frozen=True)
class GatewayConfig:
    """模型网关的接入配置。

    Attributes:
        base_url: 网关地址（含 /v1 前缀）。
        protocol: 全档位共用的协议；codex_responses 使用 Codex Responses 方言。
        profiles: 档位 → 模型。
        api_key: 平台访问网关的凭据，所有租户共用。与 ``api_key_secret`` 二选一。
        api_key_secret: 租户密钥名。给了它，就用各租户自己在保管库里存的凭据访问网关，
            网关据此按租户计费与限流。
    """

    base_url: str
    profiles: Mapping[str, ModelProfile]
    api_key: str | None = None
    api_key_secret: str | None = None
    protocol: Literal["chat", "responses", "codex_responses"] = "chat"

    def __post_init__(self) -> None:
        """三个档位都要有对应的模型；凭据来源必须且只能有一个。"""
        if self.protocol not in ("chat", "responses", "codex_responses"):
            raise ValueError("未知模型协议：仅支持 chat、responses、codex_responses")
        missing = [tier for tier in TIERS if tier not in self.profiles]
        if missing:
            raise ValueError(f"模型网关配置缺少档位：{', '.join(missing)}")
        if (self.api_key is None) == (self.api_key_secret is None):
            raise ValueError("api_key 与 api_key_secret 必须且只能给一个")


@dataclass(frozen=True)
class TierBinding:
    """一个档位绑定的客户端与模型名。"""

    client: taifeng.ModelClient
    model: str


class _PinnedSession:
    """把每次请求的模型名钉成平台选定的那个。

    内核会把 skill 声明的 ``model`` 写进请求；不改写的话，业务包里随手写的模型名会越过
    平台的路由，直接发到网关上。
    """

    def __init__(self, inner: Any, model: str) -> None:
        """包装内核客户端的会话。"""
        self._inner = inner
        self._model = model

    def stream(self, request: taifeng.ApiRequest) -> Any:
        """改写模型名后转交。"""
        return self._inner.stream(request.model_copy(update={"model": self._model}))

    async def __aenter__(self) -> _PinnedSession:
        """进入会话。"""
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """退出会话。"""
        await self._inner.__aexit__(*exc)


class TieredModelClient:
    """按档位选模型的客户端。

    内核为每轮对话建会话时会把 skill 声明的 ``model`` 传进来：是档位名就换到那个档位，
    否则用默认档位。
    """

    def __init__(self, tiers: Mapping[str, TierBinding], *, default_tier: str) -> None:
        """
        Args:
            tiers: 档位 → 客户端与模型名。
            default_tier: 业务包声明的档位。
        """
        if default_tier not in tiers:
            raise ValueError(f"默认档位 {default_tier!r} 没有对应的客户端")
        self._tiers = dict(tiers)
        self._default = default_tier
        # 能力声明跟随默认档位的客户端；没有声明的客户端由内核按纯文本处理
        capabilities = getattr(self._tiers[default_tier].client, "capabilities", None)
        if capabilities is not None:
            self.capabilities = capabilities

    def tier_for(self, requested: str | None) -> str:
        """skill 声明的 ``model`` 对应哪个档位。"""
        return requested if requested in self._tiers else self._default

    def session(self, *, cancel: taifeng.CancellationToken, model: str | None = None) -> Any:
        """创建一轮对话的会话。

        返回类型是内核的会话协议，它不在稳定层里，这里不具名引用。
        """
        binding = self._tiers[self.tier_for(model)]
        return _PinnedSession(binding.client.session(cancel=cancel), binding.model)


@runtime_checkable
class ModelClientFactory(Protocol):
    """给某个租户造模型客户端。"""

    async def create(self, tenant_id: str, tier: str) -> taifeng.ModelClient:
        """返回按档位路由的客户端，``tier`` 是默认档位。"""
        ...


class GatewayModelClientFactory:
    """经模型网关访问模型。"""

    def __init__(self, config: GatewayConfig, vault: SecretVault) -> None:
        """
        Args:
            config: 网关接入配置。
            vault: 密钥保管库；按租户取网关凭据时用。
        """
        self._config = config
        self._vault = vault

    async def _api_key(self, tenant_id: str) -> str:
        """访问网关的凭据。"""
        if self._config.api_key is not None:
            return self._config.api_key
        assert self._config.api_key_secret is not None
        return await self._vault.get(tenant_id, self._config.api_key_secret)

    async def create(self, tenant_id: str, tier: str) -> taifeng.ModelClient:
        """为租户建客户端。"""
        if tier not in TIERS:
            raise RuntimeUnavailableError(f"未知的模型档位：{tier!r}")
        api_key = await self._api_key(tenant_id)
        client_types: dict[
            str,
            type[taifeng.OpenAIChatClient]
            | type[taifeng.OpenAIResponsesClient]
            | type[taifeng.CodexResponsesClient],
        ] = {
            "chat": taifeng.OpenAIChatClient,
            "responses": taifeng.OpenAIResponsesClient,
            "codex_responses": taifeng.CodexResponsesClient,
        }
        client_type = client_types[self._config.protocol]
        tiers = {
            name: TierBinding(
                client=client_type(
                    api_key=api_key,
                    model=profile.model,
                    base_url=self._config.base_url,
                    timeout_seconds=profile.timeout_seconds,
                ),
                model=profile.model,
            )
            for name, profile in self._config.profiles.items()
            if name in TIERS
        }
        return TieredModelClient(tiers, default_tier=tier)


__all__ = [
    "TIERS",
    "GatewayConfig",
    "GatewayModelClientFactory",
    "ModelClientFactory",
    "ModelProfile",
    "TierBinding",
    "TieredModelClient",
]

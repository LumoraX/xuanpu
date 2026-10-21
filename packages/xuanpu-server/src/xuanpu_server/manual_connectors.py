"""声明式 HTTP 连接器的人工网关；独立于模型、沙盒和业务实现。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from xuanpu_spec import load_openapi

from xuanpu_server.audit import digest_arguments
from xuanpu_server.errors import InvalidRequestError, PermissionDeniedError
from xuanpu_server.runtime.http_api import HttpApiClient, resolve_base_url

if TYPE_CHECKING:
    import httpx
    from xuanpu_spec import Operation
    from xuanpu_spec.manifest import ConnectorSpec

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.packages import PackageRegistry, ResolvedPackage
    from xuanpu_server.runtime.call_context import HumanContextSigner
    from xuanpu_server.secrets import SecretVault
    from xuanpu_server.tenancy import Principal


class ManualConnectorService:
    """仅运行当前租户已安装包显式开放的人工 HTTP 操作。"""

    def __init__(self, *, packages: PackageRegistry, vault: SecretVault, audit: AuditLog,
                 signer: HumanContextSigner | None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        """注入声明解析、密钥、审计与受部署限定的人工签名器。"""
        self._packages, self._vault, self._audit = packages, vault, audit
        self._signer, self._transport = signer, transport

    async def _resolve(self, tenant: str, package: str, connector: str,
                       operation: str) -> tuple[ResolvedPackage, ConnectorSpec, Operation, str]:
        """固定到生效包版本与显式工具白名单，空白名单不扩权。"""
        resolved = await self._packages.resolve(tenant, package)
        spec = next((item for item in resolved.manifest.spec.connectors if item.name == connector), None)
        if (spec is None or spec.transport != 'http' or not spec.context_audience
                or not spec.openapi or operation not in {item.name for item in spec.tools}):
            raise PermissionDeniedError('该连接器操作未开放人工入口')
        if spec.auth is None or spec.auth.type == 'query':
            raise PermissionDeniedError('人工连接器需要通过请求头注入服务凭据')
        document = await asyncio.to_thread(load_openapi, resolved.root / spec.openapi)
        op = document.operations.get(operation)
        if op is None:
            raise PermissionDeniedError('已安装包未声明该操作')
        return resolved, spec, op, resolve_base_url(spec, document)

    async def call(self, who: Principal, package: str, connector: str, operation: str,
                   arguments: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
        """认证后只签发一次实际请求，写操作要求本次明确确认；从不自动重试。"""
        tenant = who.require_tenant()
        if (not who.user_id or who.key_id is not None or who.system is not None
                or who.role == 'platform_admin'):
            raise PermissionDeniedError('人工操作需要员工登录')
        resolved, spec, op, base_url = await self._resolve(tenant, package, connector, operation)
        if spec.effect_of(operation, read_only=op.read_only) != 'read' and not confirmed:
            raise PermissionDeniedError('该操作需要本人明确确认')
        schema = op.input_schema()
        schema['additionalProperties'] = False
        if next(Draft202012Validator(schema).iter_errors(arguments), None) is not None:
            raise InvalidRequestError('操作参数不符合已安装包的接口声明')
        if self._signer is None:
            raise PermissionDeniedError('平台未配置人工调用签名')
        interaction, call_id = 'interactive:' + str(uuid4()), str(uuid4())
        bound_signer = self._signer.bind(who, interaction, call_id)
        assert spec.context_audience is not None
        bound_signer.require_audience(base_url, spec.context_audience)
        assert spec.auth is not None
        await self._vault.get(tenant, spec.auth.secret)
        audit = {'package': package, 'package_version': resolved.record.version,
                 'package_digest': resolved.record.digest, 'connector': connector, 'operation': operation,
                 'arguments_digest': digest_arguments(arguments), 'call_id': call_id,
                 'session_id': interaction, 'origin': 'human'}
        await self._audit.control(who, tenant, 'manual_connector_started', audit)
        client = HttpApiClient(base_url=base_url, auth=spec.auth, tenant_id=tenant, vault=self._vault,
            timeout_seconds=spec.timeout_seconds, transport=self._transport,
            context_audience=spec.context_audience, context_signer=bound_signer)
        try:
            result = await client.call_structured(op, arguments)
        except Exception as exc:
            await self._audit.control(who, tenant, 'manual_connector_failed',
                                      {**audit, 'result': type(exc).__name__})
            raise
        finally:
            await client.close()
        await self._audit.control(who, tenant, 'manual_connector_completed',
            {**audit, 'status': result['status'], 'is_error': result['is_error'],
             'truncated': result['truncated']})
        return result

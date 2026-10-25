"""安装预检区分本地版本证据与事务外的对象、工具可用性。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from xuanpu_spec import archive_digest

from xuanpu_server.errors import InvalidRequestError, PlatformError, RuntimeCleanupRequiredError

from .contracts import validate_package
from .credential_bindings import BoundVault, bindings, record_digest
from .delivery import fingerprint

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.connections.store import ConnectionStore
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.objectstore import ObjectStore
    from xuanpu_server.packages import PackageRegistry
    from xuanpu_server.secrets import SecretVault

    from .delivery import Delivery
    from .schemas import PreviewInput
    from .tool_inventory import ToolInventory


class DeliveryInputError(InvalidRequestError):
    """独立严格输入错误，避免改变旧接口400契约。"""
    status = 422


def validate_config(delivery: Delivery, body: PreviewInput) -> None:
    """值必须符合已声明的类型，不能用数字代布尔或传未知字段。"""
    definitions = delivery.requirements.public_config
    if (body.public_config.keys() - definitions.keys() or
            body.secret_refs.keys() - set(delivery.requirements.secret_refs) or
            body.connection_refs.keys() - {c.connector for c in delivery.permissions.connections}):
        raise DeliveryInputError('配置包含未声明字段')
    for key, definition in definitions.items():
        if key not in body.public_config:
            continue
        value = body.public_config[key]
        expected = {'string': str, 'integer': int, 'boolean': bool}[definition.kind]
        if type(value) is not expected or (isinstance(value, str) and len(value) > 500):
            raise DeliveryInputError('公开配置类型或长度不符合声明')
        if definition.choices and value not in definition.choices:
            raise DeliveryInputError('公开配置不在声明选项中')


class Preflight:
    """缺条件形成明确阻断，供应商故障不被记成ready。"""
    def __init__(self, control: ControlPlaneStore, connections: ConnectionStore,
                 packages: PackageRegistry, objects: ObjectStore, vault: SecretVault,
                 inventory: ToolInventory) -> None:
        """对象存储与保管库来自同一平台，生产身份不来自请求体。"""
        self.control, self.connections, self.packages = control, connections, packages
        self.objects, self.vault, self.inventory = objects, vault, inventory

    async def local(self, tenant: str, delivery: Delivery, body: PreviewInput) -> tuple[dict[str, JsonValue], tuple[str, ...]]:
        """同一协调事务可复用连接读取密文和连接版本，不执行网络I/O。"""
        validate_config(delivery, body)
        missing = ['config_missing:' + name for name, definition in delivery.requirements.public_config.items()
                   if definition.required and name not in body.public_config]
        missing += ['secret_unconfigured:' + name for name in delivery.requirements.secret_refs if name not in body.secret_refs]
        credentials, absent = await bindings(self.control, tenant, body.secret_refs)
        missing.extend(absent)
        connections: dict[str, JsonValue] = {}
        for requirement in delivery.permissions.connections:
            record = await self.connections.read_record(tenant, body.connection_refs.get(requirement.connector, ''))
            if record is None or record.get('kind') != 'installation' or record.get('status') != 'active':
                missing.append('connection_unavailable:' + requirement.connector)
            elif not set(requirement.operations) <= set(record['operations']):
                missing.append('connection_scope_missing:' + requirement.connector)
            else:
                secret = await self.control.get_secret(tenant, record.get('secret_ref', ''))
                if secret is None:
                    missing.append('connection_credential_missing:' + requirement.connector)
                else:
                    connections[requirement.connector] = {'id': record['id'], 'version': record['version'],
                        'credential': record_digest(secret)}
        policy = cast('dict[str, JsonValue]', self.inventory.policy())
        if (delivery.permissions.max_tokens > self.inventory.defaults.max_session_tokens or
                delivery.permissions.max_iterations > self.inventory.defaults.max_iterations):
            missing.append('budget_exceeds_platform')
        evidence: dict[str, JsonValue] = {'credentials': cast('dict[str, JsonValue]', credentials),
            'connections': connections, 'policy': fingerprint(policy), 'delivery': fingerprint(delivery.model_dump(mode='json')),
            'public_config': body.public_config, 'secret_refs': dict(body.secret_refs), 'connection_refs': dict(body.connection_refs)}
        return evidence, tuple(sorted(set(missing)))

    async def evaluate(self, tenant: str, delivery: Delivery, body: PreviewInput,
                       *, timeout: float) -> tuple[dict[str, JsonValue], tuple[str, ...]]:
        """所有远端握手和沙盒探测在事务外，有明确超时与阻断结果。"""
        evidence, missing = await self.local(tenant, delivery, body)
        if missing:
            return evidence, missing
        try:
            async with asyncio.timeout(timeout):
                package = await self.packages.resolve(tenant, delivery.package.name, delivery.package.version)
                validate_package(delivery, package, self.inventory.defaults)
                archive = await self.objects.get(package.record.location)
                if archive_digest(archive) != delivery.package.digest:
                    return evidence, ('package_digest_mismatch',)
                credentials = cast('dict[str, str]', evidence['credentials'])
                vault = BoundVault(self.vault, body.secret_refs, credentials)
                tools, observed = await self.inventory.collect(tenant, package, vault, files=delivery.outputs.contract == 'office_files')
                actual = [t.model_dump(mode='json') for t in tools]
                expected = [t.model_dump(mode='json') for t in sorted(delivery.permissions.tools, key=lambda t: t.name)]
                evidence['tools'] = cast('list[JsonValue]', actual)
                evidence['observed_inventory'] = observed
                evidence['implementation'] = 'unverified' if any(c.transport in ('remote', 'http') for c in package.manifest.spec.connectors) else 'package_digest'
                return evidence, () if actual == expected else ('tool_inventory_changed',)
        except RuntimeCleanupRequiredError:
            raise
        except (PlatformError, OSError, TimeoutError, ValueError, TypeError):
            return evidence, ('dependency_unavailable',)

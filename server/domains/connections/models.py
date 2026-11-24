"""连接、员工同意及受保护引用的严格契约。"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Text = Annotated[str, Field(min_length=1,max_length=256)]
Operation = Literal['self_profile.read','directory_preview.read']


class Record(BaseModel):
    """拒绝未知字段并冻结公共模型。"""
    model_config = ConfigDict(extra='forbid',frozen=True,allow_inf_nan=False)


class ConnectionConfig(Record):
    """启动灰度配置；默认关闭，按租户显式启用。"""
    enabled: bool = False
    tenants: tuple[str,...] = ()
    max_consent_seconds: int = Field(default=86400,ge=60,le=86400)

    def allows(self,tenant: str) -> bool:
        """两道开关必须同时匹配。"""
        return self.enabled and tenant in self.tenants


class InstallInput(Record):
    """自建应用的本地安装，只保存凭据引用。"""
    request_id: Text
    name: Text
    corp_id: Annotated[str,Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')]
    agent_id: Annotated[str,Field(pattern=r'^[0-9]{1,20}$')]
    secret_ref: Annotated[str,Field(pattern=r'^[a-z][a-z0-9-]{0,62}$')]
    operations: tuple[Operation,...] = Field(min_length=1,max_length=2)


class ChangeInput(Record):
    """安装配置的比较交换；企业/app不可原位换绑。"""
    request_id: Text
    expected_version: int = Field(ge=1)
    status: Literal['active','disabled']
    capability_evidence: str = Field(default='',max_length=500)


class ConsentInput(Record):
    """本人在原生页面确认的有限用途同意。"""
    request_id: Text
    operations: tuple[Operation,...] = Field(min_length=1,max_length=2)
    expires_at: float
    purpose: Annotated[str,Field(min_length=1,max_length=256)]


class RevokeInput(Record):
    """撤销使用CAS与稳定请求回执。"""
    request_id: Text
    expected_version: int = Field(ge=1)


class ReadInput(Record):
    """本人读取不接受任意subject/token覆盖。"""
    operation: Operation
    parameters: dict[str,Any] = Field(default_factory=dict,max_length=0)


class ConnectionRef(Record):
    """后台仅固定引用与epoch，不携带凭据。"""
    installation_id: Text
    consent_id: Text
    installation_version: int = Field(ge=1)
    consent_epoch: int = Field(ge=1)


class PackageBindingInput(Record):
    """管理员核定不可变业务包、方法、地址及只读操作。"""
    request_id: Text
    package_digest: Annotated[str,Field(pattern=r'^[0-9a-f]{64}$')]
    connector_name: Annotated[str,Field(pattern=r'^[a-z][a-z0-9-]{0,62}$')]
    operation_id: Annotated[str,Field(pattern=r'^[A-Za-z_][A-Za-z0-9_.-]{0,100}$')]
    operation: Operation
    method: Literal['GET'] = 'GET'
    endpoint: Literal['/cgi-bin/user/get','/cgi-bin/department/list']
    effect: Literal['read'] = 'read'
    server_bound_parameters: tuple[str,...] = ()


class AccountBindingInput(Record):
    """管理员明确映射已有平台员工；不凭名称或邮箱推断。"""
    user_id: Text
    userid: Annotated[str,Field(pattern=r'^[A-Za-z0-9_.@-]{1,64}$')]
